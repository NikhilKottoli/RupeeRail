// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultDisputeTimeoutBase} from "./ScanPayVaultDisputeTimeoutBase.t.sol";

/// @dev Fuzzing of the dispute-timeout boundary, bounds and fund conservation.
contract ScanPayVaultDisputeTimeoutFuzzTest is ScanPayVaultDisputeTimeoutBase {
    /// @dev Disputes ID1 under `timeout`; returns (disputedAt, deadline).
    function _disputeWith(uint256 timeout) internal returns (uint256 disputedAt, uint256 dl) {
        _setTimeout(timeout);
        dl = _disputed(ID1);
        disputedAt = block.timestamp;
        assertEq(dl, disputedAt + timeout, "deadline = dispute time + timeout");
    }

    /// @dev Maps a seed to a time: near the deadline (-5..+5), far after it, or anywhere before it.
    function _pickTime(uint256 seed, uint256 disputedAt, uint256 dl) internal pure returns (uint256) {
        uint256 mode = seed % 3;
        uint256 r = seed >> 8;
        if (mode == 0) return dl - 5 + (r % 11);
        if (mode == 1) return dl + (r % 400 days);
        return disputedAt + (r % (dl - disputedAt));
    }

    function _tryResolve(bool refundUser) internal returns (bool ok, bytes memory ret) {
        vm.prank(arbiter);
        (ok, ret) = address(vault).call(abi.encodeCall(vault.resolveDispute, (ID1, refundUser)));
    }

    function _tryClaim(address caller) internal returns (bool ok, bytes memory ret) {
        vm.prank(caller);
        (ok, ret) = address(vault).call(abi.encodeCall(vault.claimDisputeTimeout, (ID1)));
    }

    function testFuzz_ResolveThenClaim_ExactlyOneSucceedsByBoundary(uint256 timeout, uint256 seed, bool refundUser)
        public
    {
        timeout = bound(timeout, 1 days, 30 days);
        (uint256 disputedAt, uint256 dl) = _disputeWith(timeout);
        uint256 t = _pickTime(seed, disputedAt, dl);
        vm.warp(t);
        uint256 userBefore = _bal(user);

        (bool resolveOk, bytes memory rret) = _tryResolve(refundUser);
        (bool claimOk, bytes memory cret) = _tryClaim(rando);

        assertEq(resolveOk, t < dl, "resolve succeeds iff t < deadline");
        assertTrue(resolveOk != claimOk, "exactly one of resolve/claim succeeds");
        if (resolveOk) {
            assertEq(bytes4(cret), ScanPayVault.InvalidStatus.selector, "claim after resolve: InvalidStatus");
            assertEq(_bal(user), userBefore + (refundUser ? AMT : 0));
            assertEq(vault.poolBalance(), refundUser ? 0 : AMT);
        } else {
            assertEq(bytes4(rret), ScanPayVault.DisputeResolutionExpired.selector);
            assertEq(_bal(user), userBefore + AMT);
            assertEq(vault.poolBalance(), 0);
        }
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function testFuzz_ClaimThenResolve_ExactlyOneSucceedsByBoundary(uint256 timeout, uint256 seed, bool refundUser)
        public
    {
        timeout = bound(timeout, 1 days, 30 days);
        (uint256 disputedAt, uint256 dl) = _disputeWith(timeout);
        uint256 t = _pickTime(seed, disputedAt, dl);
        vm.warp(t);

        (bool claimOk, bytes memory cret) = _tryClaim(relayer);
        (bool resolveOk, bytes memory rret) = _tryResolve(refundUser);

        assertEq(claimOk, t >= dl, "claim succeeds iff t >= deadline");
        assertTrue(resolveOk != claimOk, "never both, never neither");
        if (claimOk) {
            assertEq(bytes4(rret), ScanPayVault.InvalidStatus.selector);
            assertEq(_st(ID1), uint8(REVERSED));
        } else {
            assertEq(bytes4(cret), ScanPayVault.TooEarly.selector);
            assertEq(_st(ID1), refundUser ? uint8(REVERSED) : uint8(RELEASED));
        }
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function testFuzz_TimeoutChangeAfterDisputeNeverMovesDeadline(uint256 first, uint256 second, uint256 seed) public {
        first = bound(first, 1 days, 30 days);
        second = bound(second, 1 days, 30 days);
        (uint256 disputedAt, uint256 dl) = _disputeWith(first);
        _setTimeout(second);
        assertEq(_dl(ID1).releasableAt, dl, "snapshot unchanged");
        uint256 t = _pickTime(seed, disputedAt, dl);
        vm.warp(t);
        (bool claimOk,) = _tryClaim(rando);
        assertEq(claimOk, t >= dl, "behaviour follows the OLD deadline");
        if (!claimOk) {
            (bool resolveOk,) = _tryResolve(true);
            assertTrue(resolveOk);
        }
    }

    function testFuzz_SetDisputeTimeout_AcceptedIffWithinBounds(uint256 value) public {
        vm.prank(owner);
        (bool ok, bytes memory ret) = address(vault).call(abi.encodeCall(vault.setDisputeTimeout, (value)));
        bool inBounds = value >= 1 days && value <= 30 days;
        assertEq(ok, inBounds);
        if (inBounds) {
            assertEq(vault.disputeTimeout(), value);
        } else {
            assertEq(ret, abi.encodeWithSelector(ScanPayVault.InvalidDisputeTimeout.selector, value));
            assertEq(vault.disputeTimeout(), 7 days);
        }
    }

    /// @dev Concentrates fuzz runs around both edges.
    function testFuzz_SetDisputeTimeout_NearEdges(uint256 offset, bool upper) public {
        offset = bound(offset, 0, 3);
        uint256 value = upper ? 30 days - 1 + offset : 1 days - 2 + offset;
        vm.prank(owner);
        (bool ok,) = address(vault).call(abi.encodeCall(vault.setDisputeTimeout, (value)));
        assertEq(ok, value >= 1 days && value <= 30 days);
    }

    function testFuzz_UserReceivesExactlyAmountOnce(uint256 amount, uint256 timeout, uint256 extra, uint8 callerSel)
        public
    {
        amount = bound(amount, 1, 100 * UNIT);
        timeout = bound(timeout, 1 days, 30 days);
        extra = bound(extra, 0, 400 days);
        address[5] memory callers = [rando, relayer, settler, owner, arbiter];
        address caller = callers[callerSel % 5];
        _setTimeout(timeout);
        _lockComplete(ID1, amount);
        _dispute(ID1);
        uint256 dl = _dl(ID1).releasableAt;
        uint256 supply = token.totalSupply();
        uint256 userBefore = _bal(user);
        uint256 callerBefore = _bal(caller);

        vm.warp(dl + extra);
        _claim(caller, ID1);
        assertEq(_bal(user), userBefore + amount, "exactly amount once");
        assertEq(_bal(caller), callerBefore, "caller unpaid");
        assertEq(_bal(address(vault)), 0);
        assertEq(vault.heldTotal(), 0);
        assertEq(token.totalSupply(), supply, "no tokens created or destroyed");

        // no replay path: a second claim or any resolution reverts and moves nothing
        _expectInvalidStatus(ID1, REVERSED, DISPUTED);
        _claim(caller, ID1);
        _expectInvalidStatus(ID1, REVERSED, DISPUTED);
        _resolve(ID1, true);
        assertEq(_bal(user), userBefore + amount);
    }
}
