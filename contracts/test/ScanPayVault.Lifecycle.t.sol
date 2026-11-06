// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultBase} from "./ScanPayVaultBase.t.sol";

/// @dev complete / refund / reverse / reclaim and deadline-snapshot unit tests.
contract ScanPayVaultLifecycleTest is ScanPayVaultBase {
    uint256 internal constant AMT = 40 * UNIT;
    ScanPayVault.Status internal constant NONE = ScanPayVault.Status.NONE;
    ScanPayVault.Status internal constant LOCKED = ScanPayVault.Status.LOCKED;
    ScanPayVault.Status internal constant COMPLETED = ScanPayVault.Status.COMPLETED;
    ScanPayVault.Status internal constant REFUNDED = ScanPayVault.Status.REFUNDED;
    ScanPayVault.Status internal constant REVERSED = ScanPayVault.Status.REVERSED;

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
        assertEq(uint8(_status(ID1)), uint8(COMPLETED));
    }

    function test_Complete_WorksLongAfter() public {
        _lock(ID1, AMT);
        skip(30 days);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(uint8(_status(ID1)), uint8(COMPLETED));
    }

    function test_Complete_MovesEscrowToPoolAndEmits() public {
        ScanPayVault.OfframpIntent memory it = _lock(ID1, AMT);
        skip(20);
        uint256 vaultBal = _bal(address(vault));
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpCompleted(ID1, it.inrPaise, REF);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);

        assertEq(vault.escrowedTotal(), 0);
        assertEq(vault.poolBalance(), AMT);
        assertEq(_bal(address(vault)), vaultBal, "no token movement on complete");
        assertEq(_bal(settler), 0);
        Off memory o = _off(ID1);
        assertEq(o.completedAt, T0 + 20);
        assertEq(_dl(ID1).reversibleUntil, T0 + 20 + 24 hours);
        _assertAccounting();
    }

    function test_Complete_OnlySettler() public {
        _lock(ID1, AMT);
        skip(15);
        address[3] memory callers = [owner, user, rando];
        for (uint256 i; i < 3; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedSettler.selector, callers[i]));
            vm.prank(callers[i]);
            vault.completeOfframp(ID1, REF);
        }
    }

    function test_Complete_DoubleCompleteReverts() public {
        _lockComplete(ID1, AMT);
        _expectInvalidStatus(ID1, COMPLETED, LOCKED);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
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
        vm.prank(owner);
        vault.setPaused(true);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(uint8(_status(ID1)), uint8(COMPLETED));
    }

    function test_Complete_DelayIsReadLive() public {
        _lock(ID1, AMT);
        vm.prank(owner);
        vault.setCompleteDelay(1 days);
        skip(15);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + 1 days));
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        vm.prank(owner);
        vault.setCompleteDelay(0);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
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
            assertEq(uint8(_status(id)), uint8(REFUNDED));
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
        assertEq(uint8(_status(ID1)), uint8(LOCKED));
    }

    function test_Refund_OnlySettler() public {
        _lock(ID1, AMT);
        address[3] memory callers = [owner, user, rando];
        for (uint256 i; i < 3; i++) {
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
        vm.prank(owner);
        vault.setPaused(true);
        vm.prank(settler);
        vault.refundOfframp(ID1, 3);
        assertEq(uint8(_status(ID1)), uint8(REFUNDED));
    }

    // ------------------------------------------------------------ reverse
    function test_Reverse_PaysOriginalUserOnly() public {
        _fund(5 * UNIT); // extra pool liquidity is irrelevant
        _lockComplete(ID1, AMT);
        uint256 userBefore = _bal(user);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpReversed(ID1, user, AMT);
        vm.prank(settler);
        vault.reverseOfframp(ID1);

        assertEq(_bal(user), userBefore + AMT);
        assertEq(_bal(relayer), 0);
        assertEq(_bal(settler), 0);
        assertEq(uint8(_status(ID1)), uint8(REVERSED));
        assertEq(vault.poolBalance(), 5 * UNIT);
        assertEq(vault.escrowedTotal(), 0);
        _assertAccounting();
    }

    function test_Reverse_OnlySettler() public {
        _lockComplete(ID1, AMT);
        address[3] memory callers = [owner, user, rando];
        for (uint256 i; i < 3; i++) {
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

    function test_Reverse_WindowBoundary() public {
        _lockComplete(ID1, AMT);
        uint256 tc = _completedAt(ID1);
        vm.warp(tc + 24 hours);
        vm.prank(settler);
        vault.reverseOfframp(ID1); // ok at exact boundary
        assertEq(uint8(_status(ID1)), uint8(REVERSED));
    }

    function test_Reverse_RevertsOneSecondAfterWindow() public {
        _lockComplete(ID1, AMT);
        uint256 tc = _completedAt(ID1);
        vm.warp(tc + 24 hours + 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, tc + 24 hours));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }

    function test_Reverse_RevertsWhenPoolDrainedByOnramp() public {
        _lockComplete(ID1, AMT);
        vm.prank(settler);
        vault.onramp(keccak256("o1"), wallet, AMT, 1);
        assertEq(vault.poolBalance(), 0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, AMT));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(COMPLETED));
    }

    function test_Reverse_RevertsWhenPoolDrainedByWithdraw() public {
        _lockComplete(ID1, AMT);
        vm.prank(owner);
        vault.withdrawPool(owner, AMT - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 1, AMT));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }

    function test_Reverse_WorksAfterRefundingPool() public {
        _lockComplete(ID1, AMT);
        vm.prank(owner);
        vault.withdrawPool(owner, AMT);
        _fund(AMT);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(REVERSED));
    }

    function test_Reverse_CannotReverseTwice() public {
        _fund(AMT);
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
        assertEq(uint8(_status(ID1)), uint8(REVERSED));
        assertEq(vault.emittedInWindow(), 0);
    }

    function test_Reverse_ZeroDisputeWindow() public {
        vm.prank(owner);
        vault.setDisputeWindow(0);
        _lockComplete(ID1, AMT);
        uint256 tc = _completedAt(ID1);
        vm.warp(tc + 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, tc));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        vm.warp(tc);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }

    // ------------------------------------------------------------ reclaim
    function test_Reclaim_OnlyIntentUser() public {
        _lock(ID1, AMT);
        skip(24 hours);
        address[3] memory callers = [settler, owner, rando];
        for (uint256 i; i < 3; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedUser.selector, callers[i]));
            vm.prank(callers[i]);
            vault.reclaimOfframp(ID1);
        }
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedUser.selector, relayer));
        vm.prank(relayer);
        vault.reclaimOfframp(ID1);
    }

    function test_Reclaim_RevertsBeforeDelay() public {
        _lock(ID1, AMT);
        skip(24 hours - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + 24 hours));
        vm.prank(user);
        vault.reclaimOfframp(ID1);
    }

    function test_Reclaim_WorksAtExactly24h() public {
        _lock(ID1, AMT);
        uint256 before = _bal(user);
        skip(24 hours);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpRefunded(ID1, 5);
        vm.prank(user);
        vault.reclaimOfframp(ID1);
        assertEq(_bal(user), before + AMT);
        assertEq(uint8(_status(ID1)), uint8(REFUNDED));
        assertEq(vault.escrowedTotal(), 0);
        assertEq(vault.REASON_USER_RECLAIM(), 5);
        _assertAccounting();
    }

    function test_Reclaim_WorksWhilePaused() public {
        _lock(ID1, AMT);
        skip(24 hours);
        vm.prank(owner);
        vault.setPaused(true);
        vm.prank(user);
        vault.reclaimOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(REFUNDED));
    }

    function test_Reclaim_CannotAfterCompleteOrTwice() public {
        _lockComplete(ID1, AMT);
        skip(24 hours);
        _expectInvalidStatus(ID1, COMPLETED, LOCKED);
        vm.prank(user);
        vault.reclaimOfframp(ID1);

        _lock(ID2, AMT);
        skip(24 hours);
        vm.prank(user);
        vault.reclaimOfframp(ID2);
        _expectInvalidStatus(ID2, REFUNDED, LOCKED);
        vm.prank(user);
        vault.reclaimOfframp(ID2);
    }

    function test_Reclaim_UnknownIdReverts() public {
        _expectInvalidStatus(ID1, NONE, LOCKED);
        vm.prank(user);
        vault.reclaimOfframp(ID1);
    }

    function test_Reclaim_SettlerCannotCompleteAfterReclaim() public {
        _lock(ID1, AMT);
        skip(24 hours);
        vm.prank(user);
        vault.reclaimOfframp(ID1);
        _expectInvalidStatus(ID1, REFUNDED, LOCKED);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
    }

    // ------------------------------------------------------------ snapshots
    function test_Snapshot_ReclaimDelayChangeDoesNotAffectExistingLock() public {
        _lock(ID1, AMT);
        vm.prank(owner);
        vault.setReclaimDelay(1 hours);
        skip(1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + 24 hours));
        vm.prank(user);
        vault.reclaimOfframp(ID1);

        // new lock uses the new delay
        uint256 t = block.timestamp;
        _lock(ID2, AMT);
        assertEq(_dl(ID2).reclaimableAt, t + 1 hours);
        skip(1 hours);
        vm.prank(user);
        vault.reclaimOfframp(ID2);
    }

    function test_Snapshot_ReclaimDelayIncreaseDoesNotLockExistingLonger() public {
        _lock(ID1, AMT);
        vm.prank(owner);
        vault.setReclaimDelay(30 days);
        skip(24 hours);
        vm.prank(user);
        vault.reclaimOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(REFUNDED));
    }

    function test_Snapshot_DisputeWindowChangeAfterLock() public {
        _lock(ID1, AMT);
        vm.prank(owner);
        vault.setDisputeWindow(1 hours);
        skip(15);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        uint256 tc = _completedAt(ID1);
        assertEq(_dl(ID1).disputeWindow, 24 hours);
        assertEq(_dl(ID1).reversibleUntil, tc + 24 hours);

        // new offramp snapshots 1 hour
        _lockComplete(ID2, AMT);
        uint256 tc2 = _completedAt(ID2);
        assertEq(_dl(ID2).disputeWindow, 1 hours);
        assertEq(_dl(ID2).reversibleUntil, tc2 + 1 hours);
        vm.warp(tc2 + 1 hours + 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, tc2 + 1 hours));
        vm.prank(settler);
        vault.reverseOfframp(ID2);
        // the first one (old 24h snapshot) is still reversible
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }

    function test_Snapshot_DisputeWindowIncreaseDoesNotExtendExisting() public {
        _lock(ID1, AMT);
        vm.prank(owner);
        vault.setDisputeWindow(30 days);
        _lockComplete(ID2, AMT); // lock ID2 (30d), also warps 15s
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        uint256 tc = _completedAt(ID1);
        vm.warp(tc + 24 hours + 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, tc + 24 hours));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }
}
