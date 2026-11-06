// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultUnitBase} from "./ScanPayVaultUnitBase.t.sol";

/// @dev reclaim (permissionless, pays item.user) and deadline snapshot / delay-gap unit tests.
contract ScanPayVaultReclaimTest is ScanPayVaultUnitBase {
    // ------------------------------------------------------------ reclaim
    function test_Reclaim_RevertsBeforeDelay() public {
        _lock(ID1, AMT);
        skip(24 hours - 1);
        address[4] memory callers = [user, settler, rando, owner];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + 24 hours));
            vm.prank(callers[i]);
            vault.reclaimOfframp(ID1);
        }
        assertEq(_st(ID1), uint8(LOCKED));
    }

    function test_Reclaim_ByUserAtExactly24h() public {
        _lock(ID1, AMT);
        uint256 before = _bal(user);
        skip(24 hours);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpRefunded(ID1, 5);
        vm.prank(user);
        vault.reclaimOfframp(ID1);
        assertEq(_bal(user), before + AMT);
        assertEq(_st(ID1), uint8(REFUNDED));
        assertEq(vault.escrowedTotal(), 0);
        assertEq(vault.REASON_USER_RECLAIM(), 5);
        _assertAccounting();
    }

    function test_Reclaim_RelayedByRandoPaysUserNotCaller() public {
        _lock(ID1, AMT);
        uint256 userBefore = _bal(user);
        uint256 randoBefore = _bal(rando);
        skip(24 hours);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpRefunded(ID1, 5);
        vm.prank(rando);
        vault.reclaimOfframp(ID1);
        assertEq(_bal(user), userBefore + AMT, "user is paid");
        assertEq(_bal(rando), randoBefore, "caller is not paid");
        assertEq(_bal(address(vault)), 0);
        assertEq(_st(ID1), uint8(REFUNDED));
    }

    function test_Reclaim_BySettlerAndOwnerAndRelayerPayUser() public {
        address[3] memory callers = [settler, owner, relayer];
        for (uint256 i; i < callers.length; i++) {
            bytes32 id = bytes32(uint256(100 + i));
            _lock(id, AMT);
            uint256 userBefore = _bal(user);
            uint256 callerBefore = _bal(callers[i]);
            skip(24 hours);
            vm.prank(callers[i]);
            vault.reclaimOfframp(id);
            assertEq(_bal(user), userBefore + AMT);
            assertEq(_bal(callers[i]), callerBefore);
        }
    }

    function test_Reclaim_OneSecondBeforeFailsThenRelayedSucceeds() public {
        _lock(ID1, AMT);
        uint64 r = _dl(ID1).reclaimableAt;
        vm.warp(uint256(r) - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, uint256(r)));
        vm.prank(rando);
        vault.reclaimOfframp(ID1);
        skip(1);
        vm.prank(rando);
        vault.reclaimOfframp(ID1);
        assertEq(_st(ID1), uint8(REFUNDED));
    }

    function test_Reclaim_WorksWhilePaused() public {
        _lock(ID1, AMT);
        skip(24 hours);
        _pause();
        vm.prank(rando);
        vault.reclaimOfframp(ID1);
        assertEq(_st(ID1), uint8(REFUNDED));
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
        vm.prank(rando);
        vault.reclaimOfframp(ID1);
        _expectInvalidStatus(ID1, REFUNDED, LOCKED);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
    }

    // ------------------------------------------------------------ delay gap
    function test_SetCompleteDelay_GapBoundary() public {
        uint256 rd = vault.reclaimDelay();
        vm.startPrank(owner);
        vault.setCompleteDelay(rd - 1 hours);
        assertEq(vault.completeDelay(), rd - 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidDelayGap.selector, rd - 1 hours + 1, rd));
        vault.setCompleteDelay(rd - 1 hours + 1);
        vm.stopPrank();
        assertEq(vault.completeDelay(), rd - 1 hours);
    }

    function test_SetReclaimDelay_GapBoundary() public {
        uint256 cd = vault.completeDelay(); // 15s
        vm.startPrank(owner);
        vault.setReclaimDelay(cd + 1 hours);
        assertEq(vault.reclaimDelay(), cd + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidDelayGap.selector, cd, cd + 1 hours - 1));
        vault.setReclaimDelay(cd + 1 hours - 1);
        vm.stopPrank();
        assertEq(vault.reclaimDelay(), cd + 1 hours);
    }

    function test_SetReclaimDelay_GapAgainstLargeCompleteDelay() public {
        vm.startPrank(owner);
        vault.setReclaimDelay(2 days);
        vault.setCompleteDelay(1 days);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidDelayGap.selector, 1 days, 1 days + 1 hours - 1));
        vault.setReclaimDelay(1 days + 1 hours - 1);
        vault.setReclaimDelay(1 days + 1 hours);
        vm.stopPrank();
    }

    // ------------------------------------------------------------ snapshots
    function test_Snapshot_AllThreeDeadlinesFixedAtLock() public {
        _lock(ID1, AMT);
        Dl memory before = _dl(ID1);
        vm.startPrank(owner);
        vault.setReclaimDelay(3 days);
        vault.setCompleteDelay(2 hours);
        vault.setDisputeWindow(5 days);
        vm.stopPrank();
        Dl memory afterChange = _dl(ID1);
        assertEq(afterChange.completableAt, before.completableAt);
        assertEq(afterChange.reclaimableAt, before.reclaimableAt);
        assertEq(afterChange.disputeWindow, before.disputeWindow);
        assertEq(before.completableAt, T0 + 15);
        assertEq(before.reclaimableAt, T0 + 24 hours);
        assertEq(before.disputeWindow, 24 hours);

        // new locks use the new settings
        uint256 t = block.timestamp;
        _lock(ID2, AMT);
        Dl memory n = _dl(ID2);
        assertEq(n.completableAt, t + 2 hours);
        assertEq(n.reclaimableAt, t + 3 days);
        assertEq(n.disputeWindow, 5 days);
    }

    function test_Snapshot_ReclaimDelayShortenedDoesNotAffectExistingLock() public {
        _lock(ID1, AMT);
        vm.startPrank(owner);
        vault.setCompleteDelay(0);
        vault.setReclaimDelay(1 hours);
        vm.stopPrank();
        skip(1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + 24 hours));
        vm.prank(user);
        vault.reclaimOfframp(ID1);

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
        assertEq(_st(ID1), uint8(REFUNDED));
    }

    function test_Snapshot_DisputeWindowChangeAfterLockOnlyAffectsNewLocks() public {
        _lock(ID1, AMT);
        vm.prank(owner);
        vault.setDisputeWindow(1 hours);
        skip(15);
        _complete(ID1);
        uint256 tc = _completedAt(ID1);
        assertEq(_dl(ID1).disputeWindow, 24 hours);
        assertEq(_dl(ID1).releasableAt, tc + 24 hours);

        _lockComplete(ID2, AMT);
        uint256 tc2 = _completedAt(ID2);
        assertEq(_dl(ID2).disputeWindow, 1 hours);
        assertEq(_dl(ID2).releasableAt, tc2 + 1 hours);
        vm.warp(tc2 + 1 hours);
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
        _lock(ID2, AMT);
        skip(15);
        _complete(ID1);
        uint256 tc = _completedAt(ID1);
        vm.warp(tc + 24 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, tc + 24 hours));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        // existing lock is releasable at the old (24h) boundary
        vault.releaseOfframp(ID1);
        assertEq(_st(ID1), uint8(RELEASED));
    }
}
