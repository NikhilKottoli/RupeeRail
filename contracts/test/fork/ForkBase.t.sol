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
/// Optional env: PRIVATE_KEY = owner/settler/quote signer/relayer (all four roles, one address);
/// USER_PRIVATE_KEY = a SEPARATE user key that should hold the real token. Keys are never printed.
abstract contract ForkBase is SigHelpers {
    bytes32 internal constant DOMAIN_TYPEHASH =
        keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)");
    uint256 internal constant AMOUNT = 100_000; // 0.1 token (6 decimals)
    string internal constant QR = "upi://pay?pa=shop@upi&pn=Shop&am=8";
    bytes32 internal constant SALT = keccak256("fork-qr-salt");
    bytes32 internal constant ID1 = keccak256("fork-id1");
    bytes32 internal constant REF = keccak256("fork-payoutRef");

    ScanPayVault internal vault;
    IERC20 internal tok;
    address internal user;
    uint256 internal userPk;
    address internal owner;
    address internal settler;
    address internal arbiter;
    address internal relayer;
    address internal quoteSigner;
    uint256 internal quotePk;
    bool internal usingRealKey; // PRIVATE_KEY set
    bool internal usingRealUser; // USER_PRIVATE_KEY set

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

    /// @dev True where the token IS the native gas coin (Arc): the user's native balance mirrors the token.
    function _nativeIsToken() internal pure virtual returns (bool) {
        return false;
    }

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
        uint256 rolePk = vm.envOr("PRIVATE_KEY", uint256(0));
        uint256 realUserPk = vm.envOr("USER_PRIVATE_KEY", uint256(0));
        if (rolePk != 0 && rolePk == realUserPk) {
            vm.skip(true, "PRIVATE_KEY equals USER_PRIVATE_KEY: relayer must differ from the user");
            return;
        }
        vm.createSelectFork(url);
        _afterFork();
        assertEq(block.chainid, _chainId(), "chainid");
        tok = IERC20(_tokenAddr());
        assertEq(IERC20Metadata(_tokenAddr()).decimals(), 6, "token decimals");

        // The token's DOMAIN_SEPARATOR must equal the one recomputed from the assumed name/version.
        bytes32 reported = IERC20Permit(_tokenAddr()).DOMAIN_SEPARATOR();
        bytes32 expected = _domainSeparator(_domainVersion());
        if (reported != expected) {
            console2.log("DOMAIN MISMATCH on", _label());
            console2.log("token name():", IERC20Metadata(_tokenAddr()).name());
            console2.logBytes32(reported);
            console2.logBytes32(expected);
        }
        assertEq(reported, expected, "token DOMAIN_SEPARATOR != recomputed(name,version,chainid,token)");

        // ---- role keys ----
        if (rolePk != 0) {
            usingRealKey = true;
            owner = vm.addr(rolePk);
            settler = owner;
            relayer = owner;
            quoteSigner = owner;
            quotePk = rolePk;
            arbiter = owner;
        } else {
            owner = makeAddr("fork-owner");
            settler = makeAddr("fork-settler");
            relayer = makeAddr("fork-relayer");
            arbiter = makeAddr("fork-arbiter");
            (quoteSigner, quotePk) = makeAddrAndKey("fork-quote-signer");
        }
        // ---- user key ----
        if (realUserPk != 0) {
            usingRealUser = true;
            userPk = realUserPk;
            user = vm.addr(realUserPk);
            if (tok.balanceOf(user) < AMOUNT) {
                vm.skip(true, string.concat("USER_PRIVATE_KEY account holds < 0.1 token on ", _label()));
                return;
            }
        } else {
            (user, userPk) = makeAddrAndKey("fork-user");
            _fundUser(user, AMOUNT);
            assertEq(tok.balanceOf(user), AMOUNT, "deal() did not fund user");
        }
        assertTrue(relayer != user, "relayer must be a different address from the user");
        vault = new ScanPayVault(tok, owner, settler, quoteSigner, arbiter);
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
            qrCommitment: _commit(SALT, QR),
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

    /// @dev Locks ID1-style intents through the relayer; returns gas used by the offramp call.
    function _lock(bytes32 id) internal returns (uint256 gasUsed) {
        ScanPayVault.OfframpIntent memory it = _intent(id);
        bytes memory sig = _signIntent(vault, userPk, it);
        bytes memory qsig = _signQuote(vault, quotePk, it);
        ScanPayVault.PermitSig memory ps = _permitWithVersion(_domainVersion());
        vm.prank(relayer);
        uint256 g0 = gasleft();
        vault.offramp(it, sig, qsig, ps);
        gasUsed = g0 - gasleft();
    }

    function _status(bytes32 id) internal view returns (ScanPayVault.Status st) {
        (,,,,, st,) = vault.offramps(id);
    }

    function _lockedAt(bytes32 id) internal view returns (uint64 v) {
        (,,, v,,,) = vault.offramps(id);
    }

    function _releasableAt(bytes32 id) internal view returns (uint64 v) {
        (,,, v) = vault.deadlines(id);
    }

    function _reclaimableAt(bytes32 id) internal view returns (uint64 v) {
        (, v,,) = vault.deadlines(id);
    }

    function _lockComplete(bytes32 id) internal {
        _lock(id);
        vm.warp(block.timestamp + 15);
        vm.prank(settler);
        vault.completeOfframp(id, REF);
    }

    // ================= tests =================

    /// @dev Gasless: the user signs the permit + intent off-chain and never sends a transaction.
    function test_Fork_EndToEnd_GaslessOfframpCompleteRelease() public {
        uint256 userBefore = tok.balanceOf(user);
        uint256 relayerBefore = tok.balanceOf(relayer);
        uint64 nonceBefore = vm.getNonce(user);
        uint256 nativeBefore = user.balance;
        if (_nativeIsToken()) {
            // Arc: USDC is the gas coin, so "zero native" and "holds the token" are the same balance.
            // A real account can carry 18-decimal dust below 1e12 that the 6-decimal ERC-20 view
            // truncates, so the token view is the floor of the native balance.
            assertEq(nativeBefore / 1e12, userBefore, "arc: token balance is floor(native / 1e12)");
        } else {
            vm.deal(user, 0); // no gas token at all: the user cannot send a transaction
            assertEq(user.balance, 0);
        }

        ScanPayVault.OfframpIntent memory it = _intent(ID1);
        bytes memory sig = _signIntent(vault, userPk, it);
        bytes memory qsig = _signQuote(vault, quotePk, it);
        ScanPayVault.PermitSig memory ps = _permitWithVersion(_domainVersion());

        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpLocked(ID1, user, AMOUNT, it.inrPaise, it.rateE6, it.qrCommitment);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);

        assertEq(tok.balanceOf(user), userBefore - AMOUNT, "user debited exactly amount");
        assertEq(vm.getNonce(user), nonceBefore, "user sent no transaction");
        if (_nativeIsToken()) {
            // The user paid no gas: the native balance fell by exactly the escrowed amount (scaled), dust intact.
            assertEq(nativeBefore - user.balance, AMOUNT * 1e12, "arc: native delta is exactly amount * 1e12");
        } else assertEq(user.balance, 0, "user still has no native gas token");
        assertEq(tok.balanceOf(address(vault)), AMOUNT, "vault holds escrow");
        assertEq(tok.balanceOf(relayer), relayerBefore, "relayer token balance untouched");
        assertEq(vault.escrowedTotal(), AMOUNT);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));

        vm.warp(block.timestamp + 15);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpCompleted(ID1, it.inrPaise, REF);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(vault.heldTotal(), AMOUNT, "held after completion");
        assertEq(vault.escrowedTotal(), 0);
        assertEq(vault.poolBalance(), 0, "not in the pool yet");
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));

        // permissionless release only after the dispute window
        address anyone = makeAddr("anyone");
        uint64 rel = _releasableAt(ID1);
        vm.warp(rel - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, uint256(rel)));
        vm.prank(anyone);
        vault.releaseOfframp(ID1);
        vm.warp(rel);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpReleased(ID1, AMOUNT);
        vm.prank(anyone);
        vault.releaseOfframp(ID1);

        assertEq(vault.poolBalance(), AMOUNT, "pool credited");
        assertEq(vault.heldTotal(), 0);
        assertEq(tok.balanceOf(address(vault)), AMOUNT, "tokens stay in vault");
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.RELEASED));
        assertEq(tok.balanceOf(user), userBefore - AMOUNT, "user never gets it back");
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

    function test_Fork_ThirdPartyReclaimAfter24h() public {
        uint256 userBefore = tok.balanceOf(user);
        address third = makeAddr("third-party");
        _lock(ID1);
        uint64 reclaimAt = _reclaimableAt(ID1);
        assertEq(uint256(reclaimAt), uint256(_lockedAt(ID1)) + 24 hours);

        vm.warp(reclaimAt - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, uint256(reclaimAt)));
        vm.prank(third);
        vault.reclaimOfframp(ID1);

        vm.warp(reclaimAt);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpRefunded(ID1, 5);
        vm.prank(third);
        vault.reclaimOfframp(ID1);

        assertEq(tok.balanceOf(user), userBefore, "user (not the caller) reclaimed escrow");
        assertEq(tok.balanceOf(third), 0, "third party gets nothing");
        assertEq(vault.escrowedTotal(), 0);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REFUNDED));
    }

    function test_Fork_DisputeResolvedByArbiter_RefundsUser() public {
        uint256 userBefore = tok.balanceOf(user);
        _lockComplete(ID1);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpDisputed(ID1, user, AMOUNT);
        vm.prank(user);
        vault.disputeOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.DISPUTED));
        assertEq(vault.heldTotal(), AMOUNT, "disputed funds stay held");

        // a disputed offramp can never be released permissionlessly, even after the window
        // (releasableAt now holds the resolve deadline, so stay strictly inside the arbiter's window)
        uint256 resolveDeadline = _releasableAt(ID1);
        vm.warp(block.timestamp + vault.disputeWindow() + 1);
        assertLt(block.timestamp, resolveDeadline, "still inside the arbiter's resolve window");
        vm.expectRevert(
            abi.encodeWithSelector(
                ScanPayVault.InvalidStatus.selector, ID1, ScanPayVault.Status.DISPUTED, ScanPayVault.Status.COMPLETED
            )
        );
        vault.releaseOfframp(ID1);

        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.DisputeResolved(ID1, true, AMOUNT);
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        assertEq(tok.balanceOf(user), userBefore, "user refunded");
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 0);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REVERSED));
    }

    function test_Fork_DisputeResolvedByArbiter_ReleasesToPool() public {
        uint256 userBefore = tok.balanceOf(user);
        _lockComplete(ID1);
        vm.prank(user);
        vault.disputeOfframp(ID1);

        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.DisputeResolved(ID1, false, AMOUNT);
        vm.prank(arbiter);
        vault.resolveDispute(ID1, false);
        assertEq(tok.balanceOf(user), userBefore - AMOUNT, "user not refunded");
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), AMOUNT);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.RELEASED));
    }

    /// @dev Dispute timeout: the arbiter stays silent, the window closes and a stranger returns the funds.
    function test_Fork_DisputeTimeout_ThirdPartyClaimRefundsUser() public {
        address third = makeAddr("fork-dispute-keeper");
        assertTrue(third != user && third != relayer, "keeper must differ from user and relayer");
        uint256 userStart = tok.balanceOf(user);
        _lockComplete(ID1);
        uint256 userAfterLock = tok.balanceOf(user);
        assertEq(userAfterLock, userStart - AMOUNT, "lock debited exactly amount");
        uint256 nativeAfterLock = user.balance;

        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpDisputed(ID1, user, AMOUNT);
        vm.prank(user);
        vault.disputeOfframp(ID1);
        uint256 dl = _releasableAt(ID1); // for a DISPUTED offramp this is the resolve deadline
        assertEq(dl, block.timestamp + vault.disputeTimeout(), "deadline = dispute time + disputeTimeout");
        assertEq(vault.disputeTimeout(), 7 days);

        // before the deadline nobody but the arbiter can act
        vm.warp(dl - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, dl));
        vm.prank(third);
        vault.claimDisputeTimeout(ID1);

        // at the deadline the arbiter is locked out and the permissionless claim opens
        vm.warp(dl);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeResolutionExpired.selector, dl));
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeResolutionExpired.selector, dl));
        vm.prank(arbiter);
        vault.resolveDispute(ID1, false);

        uint256 thirdBefore = tok.balanceOf(third);
        uint256 thirdNativeBefore = third.balance;
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.DisputeTimedOut(ID1, user, AMOUNT);
        vm.prank(third);
        vault.claimDisputeTimeout(ID1);

        assertEq(tok.balanceOf(user), userStart, "user made whole: exactly amount returned");
        if (_nativeIsToken()) {
            // Arc: the token view is floor(native / 1e12); the native delta is exactly amount * 1e12.
            assertEq(user.balance - nativeAfterLock, AMOUNT * 1e12, "arc: native delta is exactly amount * 1e12");
        }
        assertEq(tok.balanceOf(third), thirdBefore, "keeper receives nothing");
        assertEq(third.balance, thirdNativeBefore, "keeper native balance untouched");
        assertEq(tok.balanceOf(address(vault)), 0, "vault emptied");
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 0);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REVERSED));

        // no replay: second claim and any later arbitration are rejected
        vm.expectRevert(
            abi.encodeWithSelector(
                ScanPayVault.InvalidStatus.selector, ID1, ScanPayVault.Status.REVERSED, ScanPayVault.Status.DISPUTED
            )
        );
        vm.prank(third);
        vault.claimDisputeTimeout(ID1);
        vm.expectRevert(
            abi.encodeWithSelector(
                ScanPayVault.InvalidStatus.selector, ID1, ScanPayVault.Status.REVERSED, ScanPayVault.Status.DISPUTED
            )
        );
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        assertEq(tok.balanceOf(user), userStart);
        assertEq(userAfterLock + AMOUNT, userStart);
    }

    function test_Fork_WrongVersionPermitMakesOfframpRevert() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1);
        bytes memory sig = _signIntent(vault, userPk, it);
        bytes memory qsig = _signQuote(vault, quotePk, it);
        ScanPayVault.PermitSig memory ps = _permitWithVersion(_wrongVersion());
        // the permit is rejected by the real token, so no allowance exists -> offramp reverts
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, uint256(0), AMOUNT));
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
    }

    function test_Fork_MissingOrWrongQuoteSignatureReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _permitWithVersion(_domainVersion());
        // wrong key signed the quote (the user is not the quote signer)
        bytes memory wrongQuote = _signQuote(vault, userPk, it);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, wrongQuote, ps);
        // missing quote signature
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, "", ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
        assertEq(tok.balanceOf(address(vault)), 0);
    }

    function test_Fork_LogOfframpGas() public {
        uint256 g = _lock(ID1);
        console2.log(string.concat("offramp gas [", _label(), "]"), g);
        assertGt(g, 0);
    }
}
