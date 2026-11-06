// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {console2} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {IERC20Metadata} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Metadata.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {ScanPayVault} from "../../src/ScanPayVault.sol";
import {SigHelpers} from "../helpers/SigHelpers.sol";

/// @dev Shared fork fixture + the full test matrix. Concrete per-chain contracts only supply config.
/// Everything skips (before any fork is created) unless the chain's RPC env var is set.
abstract contract ForkBase is SigHelpers {
    bytes32 internal constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    uint256 internal constant AMOUNT = 100_000; // 0.1 token (6 decimals)
    string internal constant QR = "upi://pay?pa=shop@upi&pn=Shop&am=8";
    bytes32 internal constant ID1 = keccak256("fork-id1");
    bytes32 internal constant REF = keccak256("fork-payoutRef");

    ScanPayVault internal vault;
    IERC20 internal tok;
    address internal user;
    uint256 internal userPk;
    address internal owner;
    address internal settler;
    address internal relayer = makeAddr("relayer");
    bool internal usingRealKey;

    // ---- per-chain config ----
    function _rpcEnv() internal pure virtual returns (string memory);
    function _chainId() internal pure virtual returns (uint256);
    function _tokenAddr() internal pure virtual returns (address);
    function _domainName() internal pure virtual returns (string memory);
    function _domainVersion() internal pure virtual returns (string memory);
    function _wrongVersion() internal pure virtual returns (string memory);
    function _label() internal pure virtual returns (string memory);

    /// @dev Hook run right after the fork is selected (e.g. to emulate chain precompiles).
    function _afterFork() internal virtual {}

    /// @dev Seeds `to` with `amount` token base units on the fork (default: storage write via deal).
    function _fundUser(address to, uint256 amount) internal virtual {
        deal(_tokenAddr(), to, amount);
    }

    function setUp() public virtual {
        string memory url = vm.envOr(_rpcEnv(), string(""));
        if (bytes(url).length == 0) {
            vm.skip(true, string.concat("fork test skipped: env var ", _rpcEnv(), " is not set"));
            return;
        }
        vm.createSelectFork(url);
        _afterFork();
        assertEq(block.chainid, _chainId(), "chainid");
        tok = IERC20(_tokenAddr());
        assertEq(IERC20Metadata(_tokenAddr()).decimals(), 6, "token decimals");

        // Section 3 domain check: token's DOMAIN_SEPARATOR must equal the one from the assumed values.
        bytes32 reported = IERC20Permit(_tokenAddr()).DOMAIN_SEPARATOR();
        bytes32 expected = _domainSeparator(_domainVersion());
        if (reported != expected) {
            console2.log("DOMAIN MISMATCH on", _label());
            console2.log("token name():", IERC20Metadata(_tokenAddr()).name());
            console2.logBytes32(reported);
            console2.logBytes32(expected);
        }
        assertEq(reported, expected, "token DOMAIN_SEPARATOR != recomputed(name,version,chainid,token)");

        uint256 pk = vm.envOr("PRIVATE_KEY", uint256(0));
        if (pk != 0) {
            usingRealKey = true;
            userPk = pk;
            user = vm.addr(pk);
            owner = user;
            settler = user;
            if (tok.balanceOf(user) < AMOUNT) {
                vm.skip(true, string.concat("PRIVATE_KEY account holds < 0.1 token on ", _label()));
                return;
            }
        } else {
            (user, userPk) = makeAddrAndKey("fork-user");
            owner = makeAddr("fork-owner");
            settler = makeAddr("fork-settler");
            _fundUser(user, AMOUNT);
            assertEq(tok.balanceOf(user), AMOUNT, "deal() did not fund user");
        }
        vault = new ScanPayVault(tok, owner, settler);
    }

    function _domainSeparator(string memory version) internal view returns (bytes32) {
        return keccak256(
            abi.encode(
                DOMAIN_TYPEHASH, keccak256(bytes(_domainName())), keccak256(bytes(version)), block.chainid, _tokenAddr()
            )
        );
    }

    function _intent(bytes32 id) internal view returns (ScanPayVault.OfframpIntent memory) {
        return ScanPayVault.OfframpIntent({
            id: id,
            user: user,
            amount: AMOUNT,
            inrPaise: 830,
            rateE6: 83_000_000,
            qrText: QR,
            deadline: uint64(block.timestamp + 1 hours)
        });
    }

    /// @dev Permit signed over an explicit domain version (so a wrong-version permit can be forged).
    function _permitWithVersion(string memory version) internal view returns (ScanPayVault.PermitSig memory ps) {
        uint256 deadline = block.timestamp + 1 hours;
        bytes32 sh = keccak256(
            abi.encode(
                PERMIT_TYPEHASH, user, address(vault), AMOUNT, IERC20Permit(_tokenAddr()).nonces(user), deadline
            )
        );
        bytes32 digest = keccak256(abi.encodePacked("\x19\x01", _domainSeparator(version), sh));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(userPk, digest);
        ps = ScanPayVault.PermitSig({deadline: deadline, v: v, r: r, s: s});
    }

    function _lock(bytes32 id) internal returns (uint256 gasUsed) {
        ScanPayVault.OfframpIntent memory it = _intent(id);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _permitWithVersion(_domainVersion());
        vm.prank(relayer);
        uint256 g0 = gasleft();
        vault.offramp(it, sig, ps);
        gasUsed = g0 - gasleft();
    }

    function _status(bytes32 id) internal view returns (ScanPayVault.Status st) {
        (,,,,, st,) = vault.offramps(id);
    }

    function _lockedAt(bytes32 id) internal view returns (uint64 v) {
        (,,, v,,,) = vault.offramps(id);
    }

    // ================= tests =================

    function test_Fork_EndToEnd_OfframpThenComplete() public {
        uint256 userBefore = tok.balanceOf(user);
        assertEq(tok.balanceOf(relayer), 0, "relayer must start empty");

        ScanPayVault.OfframpIntent memory it = _intent(ID1);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _permitWithVersion(_domainVersion());

        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpLocked(ID1, user, AMOUNT, it.inrPaise, it.rateE6, QR);
        vm.prank(relayer);
        vault.offramp(it, sig, ps);

        assertEq(tok.balanceOf(user), userBefore - AMOUNT, "user debited");
        assertEq(tok.balanceOf(address(vault)), AMOUNT, "vault holds escrow");
        assertEq(tok.balanceOf(relayer), 0, "relayer untouched");
        assertEq(vault.escrowedTotal(), AMOUNT);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));

        vm.warp(block.timestamp + 15);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpCompleted(ID1, it.inrPaise, REF);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);

        assertEq(vault.poolBalance(), AMOUNT, "pool credited");
        assertEq(vault.escrowedTotal(), 0);
        assertEq(tok.balanceOf(address(vault)), AMOUNT, "tokens stay in vault");
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));
    }

    function test_Fork_CompleteBefore15sReverts() public {
        _lock(ID1);
        uint64 lockedAt = _lockedAt(ID1);
        vm.warp(block.timestamp + 14);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, uint256(lockedAt) + 15));
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
    }

    function test_Fork_RefundReturnsTokensToUser() public {
        uint256 userBefore = tok.balanceOf(user);
        _lock(ID1);
        assertEq(tok.balanceOf(user), userBefore - AMOUNT);

        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpRefunded(ID1, 3);
        vm.prank(settler);
        vault.refundOfframp(ID1, 3);

        assertEq(tok.balanceOf(user), userBefore, "user made whole");
        assertEq(tok.balanceOf(address(vault)), 0);
        assertEq(vault.escrowedTotal(), 0);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REFUNDED));
    }

    function test_Fork_UserReclaimAfter24h() public {
        uint256 userBefore = tok.balanceOf(user);
        _lock(ID1);

        vm.warp(block.timestamp + 24 hours - 5);
        vm.prank(user);
        vm.expectRevert();
        vault.reclaimOfframp(ID1);

        vm.warp(block.timestamp + 10);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpRefunded(ID1, 5);
        vm.prank(user);
        vault.reclaimOfframp(ID1);

        assertEq(tok.balanceOf(user), userBefore, "user reclaimed escrow");
        assertEq(vault.escrowedTotal(), 0);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REFUNDED));
    }

    function test_Fork_WrongVersionPermitMakesOfframpRevert() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _permitWithVersion(_wrongVersion());
        // the permit is rejected by the real token, so no allowance exists -> offramp reverts
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, uint256(0), AMOUNT));
        vm.prank(relayer);
        vault.offramp(it, sig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
    }

    function test_Fork_LogOfframpGas() public {
        uint256 g = _lock(ID1);
        console2.log(string.concat("offramp gas [", _label(), "]"), g);
        console2.log("offramp gas", g);
        assertGt(g, 0);
    }
}
