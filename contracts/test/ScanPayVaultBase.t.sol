// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {MockPermitToken} from "./mocks/MockPermitToken.sol";
import {SigHelpers} from "./helpers/SigHelpers.sol";

/// @dev Shared fixture: vault over a 6-decimal permit token ("USDC" v2). No tests here.
abstract contract ScanPayVaultBase is SigHelpers {
    uint256 internal constant T0 = 1_700_000_000;
    uint256 internal constant UNIT = 1e6;
    string internal constant QR = "upi://pay?pa=shop@upi&pn=Shop&am=100";
    bytes32 internal constant ID1 = keccak256("id1");
    bytes32 internal constant ID2 = keccak256("id2");
    bytes32 internal constant ID3 = keccak256("id3");
    bytes32 internal constant REF = keccak256("payoutRef");
    bytes32 internal constant SALT = keccak256("qr-salt");

    ScanPayVault internal vault;
    address internal tokAddr;
    MockPermitToken internal token; // default token (same as tokAddr unless a test swaps it)

    address internal owner = makeAddr("owner");
    address internal settler = makeAddr("settler");
    address internal arbiter = makeAddr("arbiter");
    address internal guardian = makeAddr("guardian");
    address internal quoteSigner;
    uint256 internal quotePk;
    address internal relayer = makeAddr("relayer");
    address internal rando = makeAddr("rando");
    address internal wallet = makeAddr("wallet");
    address internal funder = makeAddr("funder");
    address internal user;
    uint256 internal userPk;

    function setUp() public virtual {
        vm.warp(T0);
        (user, userPk) = makeAddrAndKey("user");
        (quoteSigner, quotePk) = makeAddrAndKey("quoteSigner");
        token = new MockPermitToken("USDC", "2", 6);
        _deployVault(address(token));
        _mint(user, 10_000 * UNIT);
        _mint(funder, 10_000 * UNIT);
    }

    function _deployVault(address t) internal {
        tokAddr = t;
        vault = new ScanPayVault(IERC20(t), owner, settler, quoteSigner, arbiter);
    }

    // ---- token-agnostic helpers (work for no-return tokens too) ----
    function _mint(address to, uint256 amt) internal {
        (bool ok,) = tokAddr.call(abi.encodeWithSignature("mint(address,uint256)", to, amt));
        require(ok, "mint failed");
    }

    function _approve(address from, address spender, uint256 amt) internal {
        vm.prank(from);
        (bool ok,) = tokAddr.call(abi.encodeWithSignature("approve(address,uint256)", spender, amt));
        require(ok, "approve failed");
    }

    function _bal(address a) internal view returns (uint256) {
        return IERC20(tokAddr).balanceOf(a);
    }

    // ---- intent helpers ----
    function _intent(bytes32 id, address usr, uint256 amount, string memory qr)
        internal
        view
        returns (ScanPayVault.OfframpIntent memory)
    {
        return ScanPayVault.OfframpIntent({
            id: id,
            user: usr,
            amount: amount,
            inrPaise: 830_000,
            rateE6: 83_000_000,
            qrCommitment: _commit(SALT, qr),
            deadline: uint64(block.timestamp + 1 hours)
        });
    }

    /// @dev Builds user signature, quote signature (by quoteSigner) and permit (for it.amount to the
    /// vault). Call BEFORE vm.expectRevert.
    function _prep(ScanPayVault.OfframpIntent memory it, uint256 intentPk, uint256 permitPk)
        internal
        view
        returns (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps)
    {
        sig = _signIntent(vault, intentPk, it);
        qsig = _signQuote(vault, quotePk, it);
        ps = _signPermit(tokAddr, permitPk, address(vault), it.amount, block.timestamp + 1 hours);
    }

    /// @dev Signs the intent with `pk`, the quote with the quote signer and a matching permit from
    /// `permitPk`; relays from `relayer`.
    function _relay(ScanPayVault.OfframpIntent memory it, uint256 intentPk, uint256 permitPk) internal {
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, intentPk, permitPk);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
    }

    struct Off {
        address user;
        uint96 amount;
        uint64 inrPaise;
        uint64 lockedAt;
        uint64 completedAt;
        ScanPayVault.Status status;
        bytes32 qrCommitment;
    }

    struct Dl {
        uint64 completableAt;
        uint64 reclaimableAt;
        uint64 disputeWindow;
        uint64 releasableAt;
    }

    function _off(bytes32 id) internal view returns (Off memory o) {
        (o.user, o.amount, o.inrPaise, o.lockedAt, o.completedAt, o.status, o.qrCommitment) = vault.offramps(id);
    }

    function _dl(bytes32 id) internal view returns (Dl memory d) {
        (d.completableAt, d.reclaimableAt, d.disputeWindow, d.releasableAt) = vault.deadlines(id);
    }

    function _qr(uint256 n) internal pure returns (string memory) {
        bytes memory b = new bytes(n);
        for (uint256 i; i < n; i++) b[i] = "a";
        return string(b);
    }

    /// @dev Standard lock by `user` of `amount` with default QR.
    function _lock(bytes32 id, uint256 amount) internal returns (ScanPayVault.OfframpIntent memory it) {
        it = _intent(id, user, amount, QR);
        _relay(it, userPk, userPk);
    }

    /// @dev Unique non-zero payoutRef per id (payout refs may never be reused); ID1 keeps plain REF.
    function _refOf(bytes32 id) internal pure returns (bytes32) {
        return id == ID1 ? REF : keccak256(abi.encode(REF, id));
    }

    function _lockComplete(bytes32 id, uint256 amount) internal {
        _lock(id, amount);
        skip(15);
        vm.prank(settler);
        vault.completeOfframp(id, _refOf(id));
    }

    /// @dev Lock, complete and let the dispute window elapse, then release into the pool.
    function _lockCompleteRelease(bytes32 id, uint256 amount) internal {
        _lockComplete(id, amount);
        vm.warp(_dl(id).releasableAt);
        vault.releaseOfframp(id);
    }

    function _fund(uint256 amount) internal {
        _approve(funder, address(vault), amount);
        vm.prank(funder);
        vault.fundPool(amount);
    }

    function _status(bytes32 id) internal view returns (ScanPayVault.Status st) {
        (,,,,, st,) = vault.offramps(id);
    }

    function _lockedAt(bytes32 id) internal view returns (uint64 v) {
        (,,, v,,,) = vault.offramps(id);
    }

    function _completedAt(bytes32 id) internal view returns (uint64 v) {
        (,,,, v,,) = vault.offramps(id);
    }

    function _expectInvalidStatus(bytes32 id, ScanPayVault.Status actual, ScanPayVault.Status expected) internal {
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidStatus.selector, id, actual, expected));
    }

    function _assertAccounting() internal view {
        assertGe(
            _bal(address(vault)),
            vault.escrowedTotal() + vault.heldTotal() + vault.poolBalance(),
            "balance < escrow+held+pool"
        );
    }
}
