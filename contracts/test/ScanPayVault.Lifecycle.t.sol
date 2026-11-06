// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultUnitBase} from "./ScanPayVaultUnitBase.t.sol";

/// @dev complete / refund / reverse unit tests.
contract ScanPayVaultLifecycleTest is ScanPayVaultUnitBase {
    // ------------------------------------------------------------ complete
    function test_Complete_RevertsBeforeDelay() public {
        _lock(ID1, AMT);
        skip(14);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + 15));
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
    }

    function test_Complete_WorksAtExactlyDelay() public {
        _lock(ID1, AMT);
        skip(15);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_Complete_WorksOneSecondBeforeReclaimable() public {
        _lock(ID1, AMT);
        vm.warp(_dl(ID1).reclaimableAt - 1);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_Complete_RevertsAtAndAfterReclaimable() public {
        _lock(ID1, AMT);
        uint64 r = _dl(ID1).reclaimableAt;
        vm.warp(r);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.CompletionWindowClosed.selector, uint256(r)));
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        vm.warp(uint256(r) + 30 days);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.CompletionWindowClosed.selector, uint256(r)));
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(_st(ID1), uint8(LOCKED));
    }

    function test_Complete_MovesEscrowToHeldNotPoolAndEmits() public {
        ScanPayVault.OfframpIntent memory it = _lock(ID1, AMT);
        skip(20);
        uint256 vaultBal = _bal(address(vault));
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpCompleted(ID1, it.inrPaise, REF);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);

        assertEq(vault.escrowedTotal(), 0);
        assertEq(vault.heldTotal(), AMT);
        assertEq(vault.poolBalance(), 0, "completion must not feed the pool");
        assertEq(_bal(address(vault)), vaultBal, "no token movement on complete");
        assertEq(_bal(settler), 0);
        assertEq(_off(ID1).completedAt, T0 + 20);
        assertEq(_dl(ID1).releasableAt, T0 + 20 + 24 hours);
        assertTrue(vault.payoutRefUsed(REF));
        _assertAccounting();
    }

    function test_Complete_OnlySettler() public {
        _lock(ID1, AMT);
        skip(15);
        address[5] memory callers = [owner, user, rando, arbiter, guardian];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedSettler.selector, callers[i]));
            vm.prank(callers[i]);
            vault.completeOfframp(ID1, REF);
        }
    }

    function test_Complete_ZeroPayoutRefReverts() public {
        _lock(ID1, AMT);
        skip(15);
        vm.expectRevert(ScanPayVault.ZeroPayoutRef.selector);
        vm.prank(settler);
        vault.completeOfframp(ID1, bytes32(0));
        assertEq(_st(ID1), uint8(LOCKED));
    }

    function test_Complete_ReusedPayoutRefAcrossIdsReverts() public {
        _lock(ID1, AMT);
        _lock(ID2, AMT);
        skip(15);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.PayoutRefUsed.selector, REF));
        vm.prank(settler);
        vault.completeOfframp(ID2, REF);
        assertEq(_st(ID2), uint8(LOCKED));
        assertEq(vault.heldTotal(), AMT);
        assertEq(vault.escrowedTotal(), AMT);
        // a fresh reference works
        vm.prank(settler);
        vault.completeOfframp(ID2, keccak256("other-ref"));
        assertEq(_st(ID2), uint8(COMPLETED));
    }

    function test_Complete_DoubleCompleteReverts() public {
        _lockComplete(ID1, AMT);
        _expectInvalidStatus(ID1, COMPLETED, LOCKED);
        vm.prank(settler);
        vault.completeOfframp(ID1, keccak256("again"));
    }

    function test_Complete_UnknownIdReverts() public {
        _expectInvalidStatus(ID1, NONE, LOCKED);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
    }

    function test_Complete_AfterRefundReverts() public {
        _lock(ID1, AMT);
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        skip(15);
        _expectInvalidStatus(ID1, REFUNDED, LOCKED);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
    }

    function test_Complete_AllowedWhilePaused() public {
        _lock(ID1, AMT);
        skip(15);
        _pause();
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_Complete_UsesLockSnapshotNotLiveDelay() public {
        _lock(ID1, AMT);
        // raising the delay later does not block an existing lock
        vm.prank(owner);
        vault.setCompleteDelay(23 hours);
        skip(15);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(_st(ID1), uint8(COMPLETED));

        // lowering it later does not allow early completion of an existing lock
        vm.prank(owner);
        vault.setCompleteDelay(1 hours);
        _lock(ID2, AMT);
        vm.prank(owner);
        vault.setCompleteDelay(0);
        uint64 at = _dl(ID2).completableAt;
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, uint256(at)));
        vm.prank(settler);
        vault.completeOfframp(ID2, keccak256("r2"));
        vm.warp(at);
        vm.prank(settler);
        vault.completeOfframp(ID2, keccak256("r2"));
    }

    // ------------------------------------------------------------ refund
    function test_Refund_Reasons1To4() public {
        for (uint8 r = 1; r <= 4; r++) {
            bytes32 id = bytes32(uint256(r));
            _lock(id, AMT);
            uint256 before = _bal(user);
            vm.expectEmit(true, false, false, true, address(vault));
            emit ScanPayVault.OfframpRefunded(id, r);
            vm.prank(settler);
            vault.refundOfframp(id, r);
            assertEq(_bal(user), before + AMT);
            assertEq(_st(id), uint8(REFUNDED));
        }
        assertEq(vault.escrowedTotal(), 0);
        assertEq(_bal(address(vault)), 0);
        assertEq(_bal(settler), 0);
        assertEq(_bal(relayer), 0);
    }

    function test_Refund_InvalidReasonsRevert() public {
        _lock(ID1, AMT);
        uint8[3] memory bad = [0, 5, 255];
        for (uint256 i; i < 3; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidRefundReason.selector, bad[i]));
            vm.prank(settler);
            vault.refundOfframp(ID1, bad[i]);
        }
        assertEq(_st(ID1), uint8(LOCKED));
    }

    function test_Refund_OnlySettler() public {
        _lock(ID1, AMT);
        address[5] memory callers = [owner, user, rando, arbiter, guardian];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedSettler.selector, callers[i]));
            vm.prank(callers[i]);
            vault.refundOfframp(ID1, 1);
        }
    }

    function test_Refund_DecrementsEscrowOnly() public {
        _lock(ID1, AMT);
        _lock(ID2, 10 * UNIT);
        vm.prank(settler);
        vault.refundOfframp(ID1, 4);
        assertEq(vault.escrowedTotal(), 10 * UNIT);
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 0);
        _assertAccounting();
    }

    function test_Refund_CannotRefundCompletedOrRefunded() public {
        _lockComplete(ID1, AMT);
        _expectInvalidStatus(ID1, COMPLETED, LOCKED);
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);

        _lock(ID2, AMT);
        vm.prank(settler);
        vault.refundOfframp(ID2, 1);
        _expectInvalidStatus(ID2, REFUNDED, LOCKED);
        vm.prank(settler);
        vault.refundOfframp(ID2, 1);
    }

    function test_Refund_AllowedWhilePausedAndImmediately() public {
        _lock(ID1, AMT);
        _pause();
        vm.prank(settler);
        vault.refundOfframp(ID1, 3);
        assertEq(_st(ID1), uint8(REFUNDED));
    }

    // ------------------------------------------------------------ reverse
    function test_Reverse_PaysOriginalUserFromHeldOnly() public {
        _fund(5 * UNIT);
        _lockComplete(ID1, AMT);
        uint256 userBefore = _bal(user);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpReversed(ID1, user, AMT);
        vm.prank(settler);
        vault.reverseOfframp(ID1);

        assertEq(_bal(user), userBefore + AMT);
        assertEq(_bal(relayer), 0);
        assertEq(_bal(settler), 0);
        assertEq(_st(ID1), uint8(REVERSED));
        assertEq(vault.poolBalance(), 5 * UNIT, "pool untouched by reversal");
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.escrowedTotal(), 0);
        _assertAccounting();
    }

    function test_Reverse_OnlySettler() public {
        _lockComplete(ID1, AMT);
        address[5] memory callers = [owner, user, rando, arbiter, guardian];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedSettler.selector, callers[i]));
            vm.prank(callers[i]);
            vault.reverseOfframp(ID1);
        }
    }

    function test_Reverse_OnlyCompleted() public {
        _expectInvalidStatus(ID1, NONE, COMPLETED);
        vm.prank(settler);
        vault.reverseOfframp(ID1);

        _lock(ID1, AMT);
        _expectInvalidStatus(ID1, LOCKED, COMPLETED);
        vm.prank(settler);
        vault.reverseOfframp(ID1);

        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        _expectInvalidStatus(ID1, REFUNDED, COMPLETED);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }

    function test_Reverse_WorksOneSecondBeforeReleasable() public {
        _lockComplete(ID1, AMT);
        vm.warp(_dl(ID1).releasableAt - 1);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(_st(ID1), uint8(REVERSED));
    }

    function test_Reverse_RevertsAtReleasable() public {
        _lockComplete(ID1, AMT);
        uint64 rel = _dl(ID1).releasableAt;
        vm.warp(rel);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, uint256(rel)));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        vm.warp(uint256(rel) + 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, uint256(rel)));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }

    function test_Reverse_SucceedsAfterOwnerWithdrewEntirePool() public {
        _fund(500 * UNIT);
        vm.prank(owner);
        vault.withdrawPool(owner, 500 * UNIT);
        assertEq(vault.poolBalance(), 0);
        _lockComplete(ID1, AMT);
        uint256 userBefore = _bal(user);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(_bal(user), userBefore + AMT);
        assertEq(vault.poolBalance(), 0);
        assertEq(vault.heldTotal(), 0);
        assertEq(_bal(address(vault)), 0);
        assertEq(_st(ID1), uint8(REVERSED));
    }

    function test_Reverse_SucceedsAfterPoolDrainedByOnramp() public {
        _fund(AMT);
        _lockComplete(ID1, AMT);
        vm.prank(settler);
        vault.onramp(keccak256("o1"), wallet, AMT, 1);
        assertEq(vault.poolBalance(), 0);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(_st(ID1), uint8(REVERSED));
        assertEq(vault.poolBalance(), 0);
    }

    function test_Reverse_CannotReverseTwice() public {
        _lockComplete(ID1, AMT);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        _expectInvalidStatus(ID1, REVERSED, COMPLETED);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }

    function test_Reverse_AllowedWhilePausedAndBypassesCap() public {
        _lockComplete(ID1, AMT);
        vm.startPrank(owner);
        vault.setDailyEmitCap(0);
        vault.setPaused(true);
        vm.stopPrank();
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(_st(ID1), uint8(REVERSED));
        assertEq(vault.emittedInWindow(), 0);
    }

    function test_Reverse_MinimumOneHourWindowFloor() public {
        vm.prank(owner);
        vault.setDisputeWindow(1 hours);
        _lockComplete(ID1, AMT);
        uint256 tc = _completedAt(ID1);
        assertEq(_dl(ID1).releasableAt, tc + 1 hours);
        vm.warp(tc + 1 hours - 1);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(_st(ID1), uint8(REVERSED));
    }

    function test_Reverse_OneHourWindowExpiresAtBoundary() public {
        vm.prank(owner);
        vault.setDisputeWindow(1 hours);
        _lockComplete(ID1, AMT);
        uint256 tc = _completedAt(ID1);
        vm.warp(tc + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, tc + 1 hours));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }
}
