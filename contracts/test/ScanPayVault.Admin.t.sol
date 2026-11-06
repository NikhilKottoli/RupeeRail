// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultUnitBase} from "./ScanPayVaultUnitBase.t.sol";

/// @dev Admin setters, pause semantics, ownership and table-driven access control.
contract ScanPayVaultAdminTest is ScanPayVaultUnitBase {
    // ------------------------------------------------------------ pause
    function test_SetPaused_TogglesAndEmits() public {
        vm.expectEmit(false, false, false, true, address(vault));
        emit Pausable.Paused(owner);
        vm.prank(owner);
        vault.setPaused(true);
        assertTrue(vault.paused());
        vm.expectEmit(false, false, false, true, address(vault));
        emit Pausable.Unpaused(owner);
        vm.prank(owner);
        vault.setPaused(false);
        assertFalse(vault.paused());
    }

    function test_SetPaused_IdempotentNoRevert() public {
        vm.startPrank(owner);
        vault.setPaused(false);
        vault.setPaused(true);
        vault.setPaused(true);
        vm.stopPrank();
        assertTrue(vault.paused());
    }

    function test_SetPaused_OnlyOwner() public {
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, settler));
        vm.prank(settler);
        vault.setPaused(true);
    }

    function test_Paused_BlocksOnlyNewOfframpAndOnramp() public {
        _fund(500 * UNIT);
        _lock(ID1, 10 * UNIT); // to complete
        _lockComplete(ID2, 10 * UNIT); // to reverse
        _lock(ID3, 10 * UNIT); // to refund
        _pause();

        ScanPayVault.OfframpIntent memory it = _intent(keccak256("new"), user, UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vault.offramp(it, sig, qsig, ps);
        vm.startPrank(settler);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vault.onramp(keccak256("o"), wallet, UNIT, 1);

        skip(15);
        vault.completeOfframp(ID1, REF);
        vault.reverseOfframp(ID2);
        vault.refundOfframp(ID3, 4);
        vm.stopPrank();
        _fund(UNIT);
        vm.startPrank(owner);
        vault.withdrawPool(owner, UNIT);
        vault.sweepSurplus(owner);
        vm.stopPrank();
        _assertAccounting();
    }

    function test_Unpause_RestoresOperations() public {
        vm.startPrank(owner);
        vault.setPaused(true);
        vault.setPaused(false);
        vm.stopPrank();
        _lock(ID1, UNIT);
        assertEq(_st(ID1), uint8(LOCKED));
    }

    // ------------------------------------------------------------ limits
    function test_SetLimits_UpdatesBothAndEmits() public {
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.MaxOfframpAmountUpdated(100 * UNIT, 7 * UNIT);
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.DailyEmitCapUpdated(1000 * UNIT, 9 * UNIT);
        vm.prank(owner);
        vault.setLimits(7 * UNIT, 9 * UNIT);
        assertEq(vault.maxOfframpAmount(), 7 * UNIT);
        assertEq(vault.dailyEmitCap(), 9 * UNIT);
    }

    function test_SetLimits_AboveUint96RevertsAtomically() public {
        uint256 big = uint256(type(uint96).max) + 1;
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, big));
        vm.prank(owner);
        vault.setLimits(big, 5);
        assertEq(vault.dailyEmitCap(), 1000 * UNIT);
        assertEq(vault.maxOfframpAmount(), 100 * UNIT);
    }

    function test_SetLimits_DoesNotResetWindow() public {
        _fund(100 * UNIT);
        vm.prank(settler);
        vault.onramp(ORD(1), wallet, 10 * UNIT, 1);
        vm.prank(owner);
        vault.setLimits(50 * UNIT, 500 * UNIT);
        assertEq(vault.emittedInWindow(), 10 * UNIT);
        assertEq(vault.emitWindowStart(), T0);
        assertEq(vault.remainingDailyCap(), 490 * UNIT);
    }

    function ORD(uint256 n) internal pure returns (bytes32) {
        return keccak256(abi.encode("ord", n));
    }

    function test_SetMaxOfframpAmount_BoundsAndEvent() public {
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.MaxOfframpAmountUpdated(100 * UNIT, type(uint96).max);
        vm.startPrank(owner);
        vault.setMaxOfframpAmount(type(uint96).max);
        vm.expectRevert(
            abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, uint256(type(uint96).max) + 1)
        );
        vault.setMaxOfframpAmount(uint256(type(uint96).max) + 1);
        vm.stopPrank();
    }

    function test_SetDailyEmitCap_Event() public {
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.DailyEmitCapUpdated(1000 * UNIT, 42);
        vm.prank(owner);
        vault.setDailyEmitCap(42);
        assertEq(vault.dailyEmitCap(), 42);
    }

    // ------------------------------------------------------------ delays
    function test_SetCompleteDelay_BoundsAndEvent() public {
        vm.startPrank(owner);
        vault.setReclaimDelay(30 days); // so the 1 day bound is reachable past the 1 hour gap
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.CompleteDelayUpdated(15, 1 days);
        vault.setCompleteDelay(1 days);
        vault.setCompleteDelay(0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidCompleteDelay.selector, 1 days + 1));
        vault.setCompleteDelay(1 days + 1);
        vm.stopPrank();
        assertEq(vault.completeDelay(), 0);
    }

    function test_SetDisputeWindow_FloorBoundsAndEvent() public {
        vm.startPrank(owner);
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.DisputeWindowUpdated(24 hours, 30 days);
        vault.setDisputeWindow(30 days);
        vault.setDisputeWindow(1 hours);
        assertEq(vault.disputeWindow(), 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidDisputeWindow.selector, 1 hours - 1));
        vault.setDisputeWindow(1 hours - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidDisputeWindow.selector, 0));
        vault.setDisputeWindow(0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidDisputeWindow.selector, 30 days + 1));
        vault.setDisputeWindow(30 days + 1);
        vm.stopPrank();
        assertEq(vault.disputeWindow(), 1 hours);
    }

    function test_SetReclaimDelay_BoundsAndEvent() public {
        vm.startPrank(owner);
        vm.expectEmit(false, false, false, true, address(vault));
        emit ScanPayVault.ReclaimDelayUpdated(24 hours, 30 days);
        vault.setReclaimDelay(30 days);
        vault.setCompleteDelay(0);
        vault.setReclaimDelay(1 hours);
        assertEq(vault.reclaimDelay(), 1 hours);
        vault.setReclaimDelay(30 days);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidReclaimDelay.selector, 1 hours - 1));
        vault.setReclaimDelay(1 hours - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidReclaimDelay.selector, 30 days + 1));
        vault.setReclaimDelay(30 days + 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidReclaimDelay.selector, 0));
        vault.setReclaimDelay(0);
        vm.stopPrank();
        assertEq(vault.reclaimDelay(), 30 days);
    }

    // ------------------------------------------------------------ ownership
    function test_RenounceOwnershipReverts() public {
        vm.expectRevert(ScanPayVault.OwnershipRenunciationDisabled.selector);
        vm.prank(owner);
        vault.renounceOwnership();
        assertEq(vault.owner(), owner);
    }

    function test_Ownable2Step_TwoStepTransfer() public {
        address newOwner = makeAddr("newOwner");
        vm.prank(owner);
        vault.transferOwnership(newOwner);
        assertEq(vault.owner(), owner);
        assertEq(vault.pendingOwner(), newOwner);

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, rando));
        vm.prank(rando);
        vault.acceptOwnership();
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, owner));
        vm.prank(owner);
        vault.acceptOwnership();

        vm.expectEmit(true, true, false, false, address(vault));
        emit Ownable.OwnershipTransferred(owner, newOwner);
        vm.prank(newOwner);
        vault.acceptOwnership();
        assertEq(vault.owner(), newOwner);
        assertEq(vault.pendingOwner(), address(0));

        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, owner));
        vm.prank(owner);
        vault.setSettler(rando);
        vm.prank(newOwner);
        vault.setSettler(rando);
    }

    // ------------------------------------------------------------ access control tables
    function test_AccessControl_SettlerOnlyFunctions() public {
        bytes[4] memory calls = [
            abi.encodeCall(vault.completeOfframp, (ID1, REF)),
            abi.encodeCall(vault.refundOfframp, (ID1, uint8(1))),
            abi.encodeCall(vault.reverseOfframp, (ID1)),
            abi.encodeCall(vault.onramp, (ID1, wallet, UNIT, uint64(1)))
        ];
        address[7] memory callers = [owner, user, rando, relayer, funder, arbiter, guardian];
        for (uint256 i; i < calls.length; i++) {
            for (uint256 j; j < callers.length; j++) {
                vm.prank(callers[j]);
                (bool ok, bytes memory ret) = address(vault).call(calls[i]);
                assertFalse(ok);
                assertEq(ret, abi.encodeWithSelector(ScanPayVault.UnauthorizedSettler.selector, callers[j]));
            }
        }
    }

    function test_AccessControl_OwnerOnlyFunctions() public {
        bytes[16] memory calls = [
            abi.encodeCall(vault.withdrawPool, (wallet, 0)),
            abi.encodeCall(vault.sweepSurplus, (wallet)),
            abi.encodeCall(vault.setSettler, (wallet)),
            abi.encodeCall(vault.setQuoteSigner, (wallet)),
            abi.encodeCall(vault.setArbiter, (wallet)),
            abi.encodeCall(vault.setGuardian, (wallet)),
            abi.encodeCall(vault.setPaused, (true)),
            abi.encodeCall(vault.setLimits, (1, 1)),
            abi.encodeCall(vault.setMaxOfframpAmount, (1)),
            abi.encodeCall(vault.setDailyEmitCap, (1)),
            abi.encodeCall(vault.setCompleteDelay, (1)),
            abi.encodeCall(vault.setDisputeWindow, (1 hours)),
            abi.encodeCall(vault.setDisputeTimeout, (1 days)),
            abi.encodeCall(vault.setReclaimDelay, (1 hours)),
            abi.encodeCall(vault.renounceOwnership, ()),
            abi.encodeCall(vault.transferOwnership, (wallet))
        ];
        address[6] memory callers = [settler, rando, user, relayer, arbiter, guardian];
        for (uint256 i; i < calls.length; i++) {
            for (uint256 j; j < callers.length; j++) {
                vm.prank(callers[j]);
                (bool ok, bytes memory ret) = address(vault).call(calls[i]);
                assertFalse(ok);
                assertEq(ret, abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, callers[j]));
            }
        }
    }

    function test_AccessControl_ArbiterGuardianAndUserOnlyFunctions() public {
        address[5] memory notArbiter = [settler, owner, user, rando, guardian];
        for (uint256 j; j < notArbiter.length; j++) {
            vm.prank(notArbiter[j]);
            (bool ok, bytes memory ret) = address(vault).call(abi.encodeCall(vault.resolveDispute, (ID1, true)));
            assertFalse(ok);
            assertEq(ret, abi.encodeWithSelector(ScanPayVault.UnauthorizedArbiter.selector, notArbiter[j]));
        }
        address[5] memory notGuardian = [settler, owner, user, rando, arbiter];
        for (uint256 j; j < notGuardian.length; j++) {
            vm.prank(notGuardian[j]);
            (bool ok, bytes memory ret) = address(vault).call(abi.encodeCall(vault.guardianPause, ()));
            assertFalse(ok);
            assertEq(ret, abi.encodeWithSelector(ScanPayVault.UnauthorizedGuardian.selector, notGuardian[j]));
        }
        _lockComplete(ID1, AMT);
        address[5] memory notUser = [settler, owner, arbiter, rando, guardian];
        for (uint256 j; j < notUser.length; j++) {
            vm.prank(notUser[j]);
            (bool ok, bytes memory ret) = address(vault).call(abi.encodeCall(vault.disputeOfframp, (ID1)));
            assertFalse(ok);
            assertEq(ret, abi.encodeWithSelector(ScanPayVault.UnauthorizedUser.selector, notUser[j]));
        }
    }

    function test_EmitPaymentRemovedFromAbi() public {
        bytes memory data = abi.encodeWithSignature("emitPayment(address,uint256)", wallet, UNIT);
        vm.prank(settler);
        (bool ok,) = address(vault).call(data);
        assertFalse(ok);
    }

    // ------------------------------------------------------------ no native value
    function test_NoReceiveOrFallback() public {
        vm.deal(address(this), 1 ether);
        (bool ok,) = address(vault).call{value: 1 wei}("");
        assertFalse(ok, "receive must not exist");
        (ok,) = address(vault).call{value: 1 wei}(hex"12345678");
        assertFalse(ok, "fallback must not exist");
        (ok,) = address(vault).call(hex"12345678");
        assertFalse(ok);
        assertEq(address(vault).balance, 0);
    }

    // ------------------------------------------------------------ getters
    function test_OfframpsGetterReturnsAllSevenFields() public {
        ScanPayVault.OfframpIntent memory it = _lock(ID1, 33 * UNIT);
        (address u, uint96 amt, uint64 inr, uint64 lockedAt, uint64 completedAt, ScanPayVault.Status st, bytes32 qc) =
            vault.offramps(ID1);
        assertEq(u, user);
        assertEq(amt, 33 * UNIT);
        assertEq(inr, it.inrPaise);
        assertEq(lockedAt, T0);
        assertEq(completedAt, 0);
        assertEq(uint8(st), 1);
        assertEq(qc, it.qrCommitment);
    }

    function test_DeadlinesGetterReturnsFourFields() public {
        _lockComplete(ID1, AMT);
        (uint64 c, uint64 r, uint64 w, uint64 rel) = vault.deadlines(ID1);
        assertEq(c, T0 + 15);
        assertEq(r, T0 + 24 hours);
        assertEq(w, 24 hours);
        assertEq(rel, T0 + 15 + 24 hours);
    }

    function test_StatusEnumOrdering() public view {
        assertEq(uint8(ScanPayVault.Status.NONE), 0);
        assertEq(uint8(ScanPayVault.Status.LOCKED), 1);
        assertEq(uint8(ScanPayVault.Status.COMPLETED), 2);
        assertEq(uint8(ScanPayVault.Status.REFUNDED), 3);
        assertEq(uint8(ScanPayVault.Status.REVERSED), 4);
        assertEq(uint8(ScanPayVault.Status.RELEASED), 5);
        assertEq(uint8(ScanPayVault.Status.DISPUTED), 6);
    }

    function test_ReasonConstants() public view {
        assertEq(vault.REASON_INVALID_QR(), 1);
        assertEq(vault.REASON_AMOUNT_MISMATCH(), 2);
        assertEq(vault.REASON_DISPATCH_FAILED(), 3);
        assertEq(vault.REASON_OPERATOR(), 4);
        assertEq(vault.REASON_USER_RECLAIM(), 5);
    }
}
