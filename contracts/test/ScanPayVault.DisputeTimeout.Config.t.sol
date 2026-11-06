// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultDisputeTimeoutBase} from "./ScanPayVaultDisputeTimeoutBase.t.sol";

/// @dev Dispute timeout configuration, snapshots, role changes and keeper delivery.
contract ScanPayVaultDisputeTimeoutConfigTest is ScanPayVaultDisputeTimeoutBase {
    // ------------------------------------------------------------ (f) config changes never move a deadline
    function test_Snapshot_IncreasingTimeoutDoesNotExtendExistingDispute() public {
        uint256 dl = _disputed(ID1);
        _setTimeout(30 days);
        assertEq(_dl(ID1).releasableAt, dl, "deadline unchanged");
        vm.warp(dl - 1);
        _expectTooEarly(dl);
        _claim(rando, ID1);
        vm.warp(dl);
        _expectExpired(dl);
        _resolve(ID1, true);
        _claim(rando, ID1); // claimable at the OLD deadline even though the new timeout is 30 days
        assertEq(_st(ID1), uint8(REVERSED));
    }

    function test_Snapshot_DecreasingTimeoutDoesNotShortenExistingDispute() public {
        uint256 dl = _disputed(ID1);
        uint256 disputedAt = block.timestamp;
        _setTimeout(1 days);
        assertEq(_dl(ID1).releasableAt, dl, "deadline unchanged");
        vm.warp(disputedAt + 1 days); // the NEW timeout would already have elapsed
        _expectTooEarly(dl);
        _claim(rando, ID1);
        _resolve(ID1, false); // arbiter still has the original window
        assertEq(_st(ID1), uint8(RELEASED));
    }

    function test_Snapshot_NewDisputeUsesNewValueOldKeepsOld() public {
        uint256 dl1 = _disputed(ID1);
        _setTimeout(2 days);
        uint256 dl2 = _disputed(ID2);
        assertEq(dl2, block.timestamp + 2 days, "new dispute uses the new timeout");
        assertEq(_dl(ID1).releasableAt, dl1, "old dispute keeps its snapshot");
        _setTimeout(20 days);
        uint256 dl3 = _disputed(bytes32(uint256(77)));
        assertEq(dl3, block.timestamp + 20 days);
        assertEq(_dl(ID2).releasableAt, dl2);
        assertEq(_dl(ID1).releasableAt, dl1);
        vm.warp(dl2);
        _claim(rando, ID2);
        assertEq(_st(ID2), uint8(REVERSED));
        assertEq(_st(ID1), uint8(DISPUTED), "ID1 (7d snapshot) is still inside its window at ID2 (2d) deadline");
    }

    // ------------------------------------------------------------ (g) deadline snapshot value
    function test_Deadline_SnapshotIsDisputeTimePlusTimeoutAndOverwritesReleaseTime() public {
        _lockComplete(ID1, AMT);
        uint256 releaseTime = _dl(ID1).releasableAt;
        assertEq(releaseTime, _completedAt(ID1) + 24 hours, "COMPLETED: releasableAt is the release time");
        Dl memory before = _dl(ID1);
        skip(3 hours); // dispute some time after completion, inside the window
        uint256 disputedAt = block.timestamp;
        _dispute(ID1);
        Dl memory afterD = _dl(ID1);
        assertEq(afterD.releasableAt, disputedAt + vault.disputeTimeout(), "overwritten with resolve deadline");
        assertEq(afterD.releasableAt, disputedAt + 7 days);
        assertTrue(afterD.releasableAt != releaseTime);
        assertEq(afterD.completableAt, before.completableAt);
        assertEq(afterD.reclaimableAt, before.reclaimableAt);
        assertEq(afterD.disputeWindow, before.disputeWindow);
    }

    function test_Deadline_ReleaseAndReverseRevertInvalidStatusWhenDisputed() public {
        uint256 dl = _disputed(ID1);
        uint256[4] memory ts = [block.timestamp, dl - 1, dl, dl + 100 days];
        for (uint256 i; i < ts.length; i++) {
            vm.warp(ts[i]);
            _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
            vault.releaseOfframp(ID1);
            _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
            vm.prank(settler);
            vault.reverseOfframp(ID1);
        }
        assertEq(vault.heldTotal(), AMT);
        assertEq(vault.poolBalance(), 0);
    }

    function test_Deadline_DisputeAtLastSecondOfWindowGetsFullTimeout() public {
        _lockComplete(ID1, AMT);
        uint256 rel = _dl(ID1).releasableAt;
        vm.warp(rel - 1);
        _dispute(ID1);
        assertEq(_dl(ID1).releasableAt, rel - 1 + 7 days);
    }

    // ------------------------------------------------------------ (h) setDisputeTimeout
    function test_SetTimeout_DefaultsAndConstants() public view {
        assertEq(vault.disputeTimeout(), 7 days);
        assertEq(vault.MIN_DISPUTE_TIMEOUT(), 1 days);
        assertEq(vault.MAX_DISPUTE_TIMEOUT(), 30 days);
    }

    function test_SetTimeout_BoundsAccepted() public {
        _setTimeout(1 days);
        assertEq(vault.disputeTimeout(), 1 days);
        _setTimeout(30 days);
        assertEq(vault.disputeTimeout(), 30 days);
        _setTimeout(7 days);
        assertEq(vault.disputeTimeout(), 7 days);
    }

    function test_SetTimeout_OutOfBoundsRevert() public {
        uint256[5] memory bad = [uint256(0), 1, 1 days - 1, 30 days + 1, type(uint256).max];
        for (uint256 i; i < bad.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidDisputeTimeout.selector, bad[i]));
            vm.prank(owner);
            vault.setDisputeTimeout(bad[i]);
        }
        assertEq(vault.disputeTimeout(), 7 days, "unchanged after rejected values");
    }

    function test_SetTimeout_EmitsOldAndNew() public {
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.DisputeTimeoutUpdated(7 days, 3 days);
        _setTimeout(3 days);
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.DisputeTimeoutUpdated(3 days, 30 days);
        _setTimeout(30 days);
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.DisputeTimeoutUpdated(30 days, 30 days); // idempotent set still emits
        _setTimeout(30 days);
    }

    function test_SetTimeout_OnlyOwner() public {
        address[6] memory callers = [settler, arbiter, guardian, rando, user, relayer];
        vm.prank(owner);
        vault.setGuardian(guardian);
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, callers[i]));
            vm.prank(callers[i]);
            vault.setDisputeTimeout(2 days);
        }
        assertEq(vault.disputeTimeout(), 7 days);
    }

    function test_SetTimeout_WorksWhilePausedAndDoesNotAffectDisputeWindow() public {
        _pause();
        _setTimeout(10 days);
        assertEq(vault.disputeTimeout(), 10 days);
        assertEq(vault.disputeWindow(), 24 hours);
    }

    // ------------------------------------------------------------ (i) arbiter changes
    function test_Arbiter_NewArbiterResolvesBeforeDeadlineNobodyAfter() public {
        address newArbiter = makeAddr("newArbiter");
        uint256 dl = _disputed(ID1);
        _disputed(ID2);
        vm.prank(owner);
        vault.setArbiter(newArbiter);

        vm.warp(dl - 1);
        vm.prank(newArbiter);
        vault.resolveDispute(ID1, true);
        assertEq(_st(ID1), uint8(REVERSED));

        uint256 dl2 = _dl(ID2).releasableAt;
        vm.warp(dl2);
        _expectExpired(dl2);
        vm.prank(newArbiter);
        vault.resolveDispute(ID2, true);
        _expectExpired(dl2);
        vm.prank(newArbiter);
        vault.resolveDispute(ID2, false);
        address[3] memory others = [arbiter, owner, settler];
        for (uint256 i; i < others.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedArbiter.selector, others[i]));
            vm.prank(others[i]);
            vault.resolveDispute(ID2, true);
        }
        assertEq(_st(ID2), uint8(DISPUTED));
    }

    function test_Arbiter_DeadArbiterUserStillRecoversViaKeeper() public {
        uint256 dl = _disputed(ID1);
        vm.prank(owner);
        vault.setArbiter(address(0xdead));
        vm.warp(dl - 1);
        _expectTooEarly(dl);
        _claim(rando, ID1);
        vm.warp(dl);
        uint256 before = _bal(user);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.DisputeTimedOut(ID1, user, AMT);
        _claim(rando, ID1);
        assertEq(_bal(user), before + AMT);
        assertEq(_bal(address(0xdead)), 0);
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function test_Arbiter_CompromisedArbiterCannotReleaseAfterDeadline() public {
        // a compromised arbiter key is limited to the window: after it, release-to-pool is impossible
        uint256 dl = _disputed(ID1);
        vm.warp(dl);
        _expectExpired(dl);
        _resolve(ID1, false);
        assertEq(vault.poolBalance(), 0);
        _claim(rando, ID1);
        assertEq(vault.poolBalance(), 0);
        assertEq(_bal(arbiter), 0);
    }

    // ------------------------------------------------------------ (k) keeper delivers to a zero-balance user
    function test_Keeper_DeliversRefundToZeroNativeBalanceUser() public {
        address keeper = makeAddr("keeper");
        vm.deal(user, 1 ether); // the user needs gas once, to open the dispute
        uint256 dl = _disputed(ID1);
        vm.deal(user, 0);
        vm.warp(dl);
        uint64 nonceBefore = vm.getNonce(user);
        uint256 before = _bal(user);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.DisputeTimedOut(ID1, user, AMT);
        _claim(keeper, ID1);
        assertEq(_bal(user), before + AMT, "refund delivered");
        assertEq(user.balance, 0, "user still holds no native token");
        assertEq(vm.getNonce(user), nonceBefore, "user sent no transaction");
        assertEq(_bal(keeper), 0);
        assertEq(_st(ID1), uint8(REVERSED));
    }
}
