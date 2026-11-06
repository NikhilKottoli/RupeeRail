// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultUnitBase} from "./ScanPayVaultUnitBase.t.sol";

/// @dev dispute / release / resolveDispute unit tests (held-funds lifecycle).
contract ScanPayVaultDisputeTest is ScanPayVaultUnitBase {
    // ------------------------------------------------------------ dispute
    function test_Dispute_FreezesHeldAndEmits() public {
        _lockComplete(ID1, AMT);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpDisputed(ID1, user, AMT);
        _dispute(ID1);
        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(vault.heldTotal(), AMT, "still held");
        assertEq(vault.poolBalance(), 0);
        assertEq(_bal(address(vault)), AMT);
        assertEq(_dl(ID1).releasableAt, block.timestamp + vault.disputeTimeout(), "resolve deadline snapshotted");
        _assertAccounting();
    }

    function test_Dispute_OnlyItemUser() public {
        _lockComplete(ID1, AMT);
        address[6] memory callers = [settler, owner, arbiter, rando, relayer, guardian];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedUser.selector, callers[i]));
            vm.prank(callers[i]);
            vault.disputeOfframp(ID1);
        }
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_Dispute_WorksOneSecondBeforeReleasable() public {
        _lockComplete(ID1, AMT);
        vm.warp(_dl(ID1).releasableAt - 1);
        _dispute(ID1);
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    function test_Dispute_RevertsAtExactlyReleasableAt() public {
        _lockComplete(ID1, AMT);
        uint64 rel = _dl(ID1).releasableAt;
        vm.warp(rel);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, uint256(rel)));
        vm.prank(user);
        vault.disputeOfframp(ID1);
        vm.warp(uint256(rel) + 1 days);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, uint256(rel)));
        vm.prank(user);
        vault.disputeOfframp(ID1);
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_Dispute_OnlyOnCompleted() public {
        // NONE
        _expectInvalidStatus(ID1, NONE, COMPLETED);
        vm.prank(user);
        vault.disputeOfframp(ID1);
        // LOCKED
        _lock(ID1, AMT);
        _expectInvalidStatus(ID1, LOCKED, COMPLETED);
        vm.prank(user);
        vault.disputeOfframp(ID1);
        // REFUNDED
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        _expectInvalidStatus(ID1, REFUNDED, COMPLETED);
        vm.prank(user);
        vault.disputeOfframp(ID1);
        // DISPUTED (twice)
        _lockCompleteDispute(ID2, AMT);
        _expectInvalidStatus(ID2, DISPUTED, COMPLETED);
        vm.prank(user);
        vault.disputeOfframp(ID2);
        // REVERSED
        _lockComplete(ID3, AMT);
        vm.prank(settler);
        vault.reverseOfframp(ID3);
        _expectInvalidStatus(ID3, REVERSED, COMPLETED);
        vm.prank(user);
        vault.disputeOfframp(ID3);
    }

    function test_Dispute_NotOnReleased() public {
        _lockCompleteRelease(ID1, AMT);
        _expectInvalidStatus(ID1, RELEASED, COMPLETED);
        vm.prank(user);
        vault.disputeOfframp(ID1);
    }

    function test_Dispute_BlocksReleaseEvenAfterWindow() public {
        _lockCompleteDispute(ID1, AMT);
        vm.warp(_dl(ID1).releasableAt + 365 days);
        _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
        vm.prank(rando);
        vault.releaseOfframp(ID1);
        assertEq(vault.poolBalance(), 0);
        assertEq(vault.heldTotal(), AMT);
    }

    function test_Dispute_BlocksReverse() public {
        _lockCompleteDispute(ID1, AMT);
        _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    function test_Dispute_BlocksRefundAndReclaim() public {
        _lockCompleteDispute(ID1, AMT);
        skip(30 days);
        _expectInvalidStatus(ID1, DISPUTED, LOCKED);
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        _expectInvalidStatus(ID1, DISPUTED, LOCKED);
        vm.prank(rando);
        vault.reclaimOfframp(ID1);
    }

    // ------------------------------------------------------------ release
    function test_Release_RevertsOneSecondBeforeReleasable() public {
        _lockComplete(ID1, AMT);
        uint64 rel = _dl(ID1).releasableAt;
        vm.warp(uint256(rel) - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, uint256(rel)));
        vm.prank(rando);
        vault.releaseOfframp(ID1);
        assertEq(_st(ID1), uint8(COMPLETED));
        assertEq(vault.poolBalance(), 0);
    }

    function test_Release_PermissionlessExactlyAtReleasableMovesHeldToPool() public {
        _lockComplete(ID1, AMT);
        uint64 rel = _dl(ID1).releasableAt;
        vm.warp(rel);
        uint256 vaultBal = _bal(address(vault));
        uint256 randoBal = _bal(rando);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.OfframpReleased(ID1, AMT);
        vm.prank(rando);
        vault.releaseOfframp(ID1);
        assertEq(_st(ID1), uint8(RELEASED));
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), AMT);
        assertEq(_bal(address(vault)), vaultBal, "release moves no tokens");
        assertEq(_bal(rando), randoBal);
        _assertAccounting();
    }

    function test_Release_AnyCallerIncludingSettlerOwnerUser() public {
        address[4] memory callers = [settler, owner, user, relayer];
        for (uint256 i; i < callers.length; i++) {
            bytes32 id = bytes32(uint256(200 + i));
            _lockComplete(id, UNIT);
            vm.warp(_dl(id).releasableAt);
            vm.prank(callers[i]);
            vault.releaseOfframp(id);
            assertEq(_st(id), uint8(RELEASED));
        }
        assertEq(vault.poolBalance(), 4 * UNIT);
    }

    function test_Release_OnlyOnCompletedAndNotTwice() public {
        _expectInvalidStatus(ID1, NONE, COMPLETED);
        vault.releaseOfframp(ID1);
        _lock(ID1, AMT);
        _expectInvalidStatus(ID1, LOCKED, COMPLETED);
        vault.releaseOfframp(ID1);
        _lockCompleteRelease(ID2, AMT);
        _expectInvalidStatus(ID2, RELEASED, COMPLETED);
        vault.releaseOfframp(ID2);
    }

    function test_Release_ThenReverseRevertsInvalidStatus() public {
        _lockCompleteRelease(ID1, AMT);
        _expectInvalidStatus(ID1, RELEASED, COMPLETED);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
    }

    function test_Release_FundsEnterPoolAndOnrampCanSpendThem() public {
        _lockCompleteRelease(ID1, AMT);
        vm.prank(settler);
        vault.onramp(keccak256("o"), wallet, AMT, 1);
        assertEq(_bal(wallet), AMT);
        assertEq(vault.poolBalance(), 0);
        _assertAccounting();
    }

    // ------------------------------------------------------------ resolveDispute
    function test_Resolve_RefundPaysItemUserAndReverses() public {
        _fund(7 * UNIT);
        _lockCompleteDispute(ID1, AMT);
        uint256 userBefore = _bal(user);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.DisputeResolved(ID1, true, AMT);
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        assertEq(_bal(user), userBefore + AMT);
        assertEq(_bal(arbiter), 0);
        assertEq(_st(ID1), uint8(REVERSED));
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 7 * UNIT);
        _assertAccounting();
    }

    function test_Resolve_ReleaseMovesHeldToPool() public {
        _lockCompleteDispute(ID1, AMT);
        uint256 userBefore = _bal(user);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.DisputeResolved(ID1, false, AMT);
        vm.prank(arbiter);
        vault.resolveDispute(ID1, false);
        assertEq(_bal(user), userBefore);
        assertEq(_st(ID1), uint8(RELEASED));
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), AMT);
        _assertAccounting();
    }

    function test_Resolve_OnlyArbiter() public {
        _lockCompleteDispute(ID1, AMT);
        address[6] memory callers = [settler, owner, user, rando, guardian, relayer];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedArbiter.selector, callers[i]));
            vm.prank(callers[i]);
            vault.resolveDispute(ID1, true);
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedArbiter.selector, callers[i]));
            vm.prank(callers[i]);
            vault.resolveDispute(ID1, false);
        }
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    function test_Resolve_OnlyOnDisputed() public {
        vm.startPrank(arbiter);
        _expectInvalidStatus(ID1, NONE, DISPUTED);
        vault.resolveDispute(ID1, true);
        vm.stopPrank();

        _lock(ID1, AMT);
        _expectInvalidStatus(ID1, LOCKED, DISPUTED);
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);

        _lockComplete(ID2, AMT);
        _expectInvalidStatus(ID2, COMPLETED, DISPUTED);
        vm.prank(arbiter);
        vault.resolveDispute(ID2, false);

        _lockCompleteRelease(ID3, AMT);
        _expectInvalidStatus(ID3, RELEASED, DISPUTED);
        vm.prank(arbiter);
        vault.resolveDispute(ID3, true);
    }

    function test_Resolve_CannotResolveTwice() public {
        _lockCompleteDispute(ID1, AMT);
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        _expectInvalidStatus(ID1, REVERSED, DISPUTED);
        vm.prank(arbiter);
        vault.resolveDispute(ID1, false);
    }

    /// @dev Dispute window elapsing (24h) does not close arbitration: the arbiter has the full disputeTimeout.
    function test_Resolve_WorksAfterDisputeWindowButBeforeResolveDeadline() public {
        _lockCompleteDispute(ID1, AMT);
        _lockCompleteDispute(ID2, AMT);
        vm.warp(_dl(ID1).releasableAt - 1); // last second before ID1's resolve deadline (ID2's is later)
        uint256 userBefore = _bal(user);
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        vm.prank(arbiter);
        vault.resolveDispute(ID2, false);
        assertEq(_bal(user), userBefore + AMT);
        assertEq(vault.poolBalance(), AMT);
        assertEq(vault.heldTotal(), 0);
    }

    /// @dev The arbiter can no longer resolve "years later": the resolve deadline closes arbitration and
    /// anyone then returns the funds to the user via claimDisputeTimeout.
    function test_Resolve_YearsLaterRevertsExpiredAndClaimPaysUser() public {
        _lockCompleteDispute(ID1, AMT);
        _lockCompleteDispute(ID2, AMT);
        uint256 dl = _dl(ID1).releasableAt;
        uint256 dl2 = _dl(ID2).releasableAt;
        skip(5 * 365 days);
        uint256 userBefore = _bal(user);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeResolutionExpired.selector, dl));
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeResolutionExpired.selector, dl2));
        vm.prank(arbiter);
        vault.resolveDispute(ID2, false);
        assertEq(vault.heldTotal(), 2 * AMT);
        vault.claimDisputeTimeout(ID1);
        vault.claimDisputeTimeout(ID2);
        assertEq(_bal(user), userBefore + 2 * AMT);
        assertEq(vault.poolBalance(), 0);
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function test_Resolve_WorksWhilePaused() public {
        _lockCompleteDispute(ID1, AMT);
        _pause();
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        assertEq(_st(ID1), uint8(REVERSED));
    }

    // ------------------------------------------------------------ paused behaviour
    function test_Paused_ReleaseDisputeReverseResolveStillWork() public {
        _lockComplete(ID1, AMT); // will be reversed
        _lockComplete(ID2, AMT); // will be disputed then resolved
        _lockComplete(ID3, AMT); // will be released
        _pause();

        vm.prank(settler);
        vault.reverseOfframp(ID1);
        _dispute(ID2);
        assertEq(_st(ID1), uint8(REVERSED));
        assertEq(_st(ID2), uint8(DISPUTED));

        vm.warp(_dl(ID3).releasableAt);
        vm.prank(rando);
        vault.releaseOfframp(ID3);
        vm.prank(arbiter);
        vault.resolveDispute(ID2, false);
        assertEq(_st(ID3), uint8(RELEASED));
        assertEq(_st(ID2), uint8(RELEASED));
        assertEq(vault.poolBalance(), 2 * AMT);
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function test_Paused_OfframpAndOnrampRevert() public {
        _fund(10 * UNIT);
        _pause();
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vault.offramp(it, sig, qsig, ps);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vm.prank(settler);
        vault.onramp(keccak256("o"), wallet, UNIT, 1);
    }
}
