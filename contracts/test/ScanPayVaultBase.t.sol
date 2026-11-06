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

    ScanPayVault internal vault;
    address internal tokAddr;
    MockPermitToken internal token; // default token (same as tokAddr unless a test swaps it)

    address internal owner = makeAddr("owner");
    address internal settler = makeAddr("settler");
    address internal relayer = makeAddr("relayer");
    address internal rando = makeAddr("rando");
    address internal wallet = makeAddr("wallet");
    address internal funder = makeAddr("funder");
    address internal user;
    uint256 internal userPk;

    function setUp() public virtual {
        vm.warp(T0);
        (user, userPk) = makeAddrAndKey("user");
        token = new MockPermitToken("USDC", "2", 6);
        _deployVault(address(token));
        _mint(user, 10_000 * UNIT);
        _mint(funder, 10_000 * UNIT);
    }

    function _deployVault(address t) internal {
        tokAddr = t;
        vault = new ScanPayVault(IERC20(t), owner, settler);
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
            qrText: qr,
            deadline: uint64(block.timestamp + 1 hours)
        });
    }

    /// @dev Builds intent signature + permit (for it.amount to the vault). Call BEFORE vm.expectRevert.
    function _prep(ScanPayVault.OfframpIntent memory it, uint256 intentPk, uint256 permitPk)
        internal
        view
        returns (bytes memory sig, ScanPayVault.PermitSig memory ps)
    {
        sig = _signIntent(vault, intentPk, it);
        ps = _signPermit(tokAddr, permitPk, address(vault), it.amount, block.timestamp + 1 hours);
    }

    /// @dev Signs the intent with `pk` and a matching permit from `permitPk`; relays from `relayer`.
    function _relay(ScanPayVault.OfframpIntent memory it, uint256 intentPk, uint256 permitPk) internal {
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, intentPk, permitPk);
        vm.prank(relayer);
        vault.offramp(it, sig, ps);
    }

    struct Off {
        address user;
        uint96 amount;
        uint64 inrPaise;
        uint64 lockedAt;
        uint64 completedAt;
        ScanPayVault.Status status;
        bytes32 qrHash;
    }

    struct Dl {
        uint64 reclaimableAt;
        uint64 reversibleUntil;
        uint64 disputeWindow;
    }

    function _off(bytes32 id) internal view returns (Off memory o) {
        (o.user, o.amount, o.inrPaise, o.lockedAt, o.completedAt, o.status, o.qrHash) = vault.offramps(id);
    }

    function _dl(bytes32 id) internal view returns (Dl memory d) {
        (d.reclaimableAt, d.reversibleUntil, d.disputeWindow) = vault.deadlines(id);
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

    function _lockComplete(bytes32 id, uint256 amount) internal {
        _lock(id, amount);
        skip(15);
        vm.prank(settler);
        vault.completeOfframp(id, REF);
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
        assertEq(_bal(address(vault)), vault.escrowedTotal() + vault.poolBalance(), "balance != escrow+pool");
    }
}
