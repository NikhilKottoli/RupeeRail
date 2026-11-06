// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultUnitBase} from "./ScanPayVaultUnitBase.t.sol";

/// @dev onramp / daily cap / fundPool / withdrawPool / sweepSurplus unit tests.
contract ScanPayVaultPayoutTest is ScanPayVaultUnitBase {
    bytes32 internal constant ORD1 = keccak256("order1");
    bytes32 internal constant ORD2 = keccak256("order2");

    function setUp() public override {
        super.setUp();
        _fund(2_000 * UNIT);
    }

    function _drainPool() internal {
        uint256 pool = vault.poolBalance();
        vm.prank(owner);
        vault.withdrawPool(owner, pool);
    }

    function _on(uint256 n, uint256 amt) internal {
        vm.prank(settler);
        vault.onramp(keccak256(abi.encode("ord", n)), wallet, amt, 1);
    }

    // ------------------------------------------------------------ onramp
    function test_Onramp_PaysWalletAndEmits() public {
        uint256 pool = vault.poolBalance();
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OnrampPaid(ORD1, wallet, 30 * UNIT, 2_490_000);
        vm.prank(settler);
        vault.onramp(ORD1, wallet, 30 * UNIT, 2_490_000);

        assertEq(_bal(wallet), 30 * UNIT);
        assertEq(vault.poolBalance(), pool - 30 * UNIT);
        assertEq(_bal(settler), 0);
        assertTrue(vault.onrampDone(ORD1));
        assertFalse(vault.onrampDone(ORD2));
        assertEq(vault.emittedInWindow(), 30 * UNIT);
        _assertAccounting();
    }

    function test_Onramp_OnlySettler() public {
        address[5] memory callers = [owner, user, rando, arbiter, guardian];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedSettler.selector, callers[i]));
            vm.prank(callers[i]);
            vault.onramp(ORD1, wallet, UNIT, 1);
        }
    }

    function test_Onramp_RepeatedOrderIdReverts() public {
        vm.startPrank(settler);
        vault.onramp(ORD1, wallet, UNIT, 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OnrampAlreadyPaid.selector, ORD1));
        vault.onramp(ORD1, wallet, UNIT, 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OnrampAlreadyPaid.selector, ORD1));
        vault.onramp(ORD1, rando, 5 * UNIT, 99);
        vm.stopPrank();
        assertEq(_bal(rando), 0);
    }

    function test_Onramp_InvalidInputsRevert() public {
        vm.startPrank(settler);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidRecipient.selector, address(0)));
        vault.onramp(ORD1, address(0), UNIT, 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidRecipient.selector, address(vault)));
        vault.onramp(ORD1, address(vault), UNIT, 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidAmount.selector, 0));
        vault.onramp(ORD1, wallet, 0, 1);
        uint256 pool = vault.poolBalance();
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, pool, pool + 1));
        vault.onramp(ORD1, wallet, pool + 1, 1);
        vm.stopPrank();
        assertFalse(vault.onrampDone(ORD1), "failed payout must not burn orderId");
    }

    function test_Onramp_PausedReverts() public {
        _pause();
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vm.prank(settler);
        vault.onramp(ORD1, wallet, UNIT, 1);
    }

    function test_Onramp_CannotUseEscrowOrHeld() public {
        _drainPool();
        _lock(ID1, 50 * UNIT);
        _lockComplete(ID2, 20 * UNIT);
        assertEq(_bal(address(vault)), 70 * UNIT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, UNIT));
        vm.prank(settler);
        vault.onramp(ORD1, wallet, UNIT, 1);
        assertEq(vault.escrowedTotal(), 50 * UNIT);
        assertEq(vault.heldTotal(), 20 * UNIT);
    }

    // ------------------------------------------------------------ daily cap
    function test_Cap_ConsumedByOnramps() public {
        assertEq(vault.remainingDailyCap(), 1000 * UNIT);
        _on(1, 600 * UNIT);
        assertEq(vault.remainingDailyCap(), 400 * UNIT);
        _on(2, 400 * UNIT);
        assertEq(vault.remainingDailyCap(), 0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vm.prank(settler);
        vault.onramp(ORD2, wallet, 1, 1);
        assertFalse(vault.onrampDone(ORD2));
    }

    function test_Cap_ExceedReportsRemaining() public {
        _on(1, 990 * UNIT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 10 * UNIT, 10 * UNIT + 1));
        vm.prank(settler);
        vault.onramp(ORD1, wallet, 10 * UNIT + 1, 1);
        _on(2, 10 * UNIT); // exactly remaining is fine
    }

    function test_Cap_FixedWindowBoundary() public {
        _on(1, 1000 * UNIT);
        vm.warp(T0 + 1 days - 1);
        assertEq(vault.remainingDailyCap(), 0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vm.prank(settler);
        vault.onramp(ORD1, wallet, 1, 1);

        vm.warp(T0 + 1 days);
        assertEq(vault.remainingDailyCap(), 1000 * UNIT);
        assertEq(vault.emittedInWindow(), 1000 * UNIT); // storage not rolled yet
        _on(2, 300 * UNIT);
        assertEq(vault.emitWindowStart(), T0 + 1 days);
        assertEq(vault.emittedInWindow(), 300 * UNIT);
        assertEq(vault.remainingDailyCap(), 700 * UNIT);
    }

    function test_Cap_WindowIsFixedNotRolling() public {
        vm.warp(T0 + 23 hours);
        _on(1, 500 * UNIT);
        assertEq(vault.emitWindowStart(), T0, "payout inside window must not move its start");
        vm.warp(T0 + 24 hours);
        assertEq(vault.remainingDailyCap(), 1000 * UNIT);
    }

    function test_Cap_LoweringBelowUsedGivesZeroRemaining() public {
        _on(1, 500 * UNIT);
        vm.prank(owner);
        vault.setDailyEmitCap(100 * UNIT);
        assertEq(vault.remainingDailyCap(), 0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vm.prank(settler);
        vault.onramp(ORD1, wallet, 1, 1);
        vm.prank(owner);
        vault.setDailyEmitCap(600 * UNIT);
        assertEq(vault.remainingDailyCap(), 100 * UNIT);
    }

    function test_Cap_ZeroCapDisablesPayouts() public {
        vm.prank(owner);
        vault.setDailyEmitCap(0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vm.prank(settler);
        vault.onramp(ORD1, wallet, 1, 1);
    }

    function test_Cap_FailedPayoutDoesNotConsumeCap() public {
        uint256 pool = vault.poolBalance();
        _on(1, 100 * UNIT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, pool - 100 * UNIT, pool));
        vm.prank(settler);
        vault.onramp(ORD1, wallet, pool, 1);
        assertEq(vault.emittedInWindow(), 100 * UNIT);
        assertEq(vault.remainingDailyCap(), 900 * UNIT);
    }

    function test_Cap_FailedPayoutInNewWindowDoesNotRollWindow() public {
        _on(1, 100 * UNIT);
        vm.warp(T0 + 2 days);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 1000 * UNIT, 1001 * UNIT));
        vm.prank(settler);
        vault.onramp(ORD1, wallet, 1001 * UNIT, 1);
        assertEq(vault.emitWindowStart(), T0);
        assertEq(vault.emittedInWindow(), 100 * UNIT);
    }

    // ------------------------------------------------------------ fundPool
    function test_FundPool_AnyoneAnyTimeEvent() public {
        uint256 pool = vault.poolBalance();
        _mint(rando, 9 * UNIT);
        _approve(rando, address(vault), 9 * UNIT);
        _pause(); // funding allowed while paused
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.PoolFunded(rando, 9 * UNIT);
        vm.prank(rando);
        vault.fundPool(9 * UNIT);
        assertEq(vault.poolBalance(), pool + 9 * UNIT);
        assertEq(_bal(rando), 0);
        _assertAccounting();
    }

    function test_FundPool_ZeroAmountReverts() public {
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidAmount.selector, 0));
        vm.prank(funder);
        vault.fundPool(0);
    }

    function test_FundPool_NoAllowanceReverts() public {
        _approve(funder, address(vault), 0);
        vm.expectRevert();
        vm.prank(funder);
        vault.fundPool(UNIT);
    }

    function test_FundPool_DirectTransferIsUnaccountedSurplus() public {
        uint256 pool = vault.poolBalance();
        vm.prank(funder);
        token.transfer(address(vault), 5 * UNIT);
        assertEq(vault.poolBalance(), pool);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, pool, pool + 1));
        vm.prank(owner);
        vault.withdrawPool(owner, pool + 1);
    }

    // ------------------------------------------------------------ withdrawPool
    function test_Withdraw_OwnerPaysRecipientAndEmits() public {
        uint256 pool = vault.poolBalance();
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.PoolWithdrawn(rando, 123 * UNIT);
        vm.prank(owner);
        vault.withdrawPool(rando, 123 * UNIT);
        assertEq(_bal(rando), 123 * UNIT);
        assertEq(vault.poolBalance(), pool - 123 * UNIT);
        _assertAccounting();
    }

    function test_Withdraw_OnlyOwner() public {
        address[5] memory callers = [settler, rando, user, arbiter, guardian];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, callers[i]));
            vm.prank(callers[i]);
            vault.withdrawPool(callers[i], UNIT);
        }
    }

    function test_Withdraw_CannotExceedPool() public {
        uint256 pool = vault.poolBalance();
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, pool, pool + 1));
        vm.prank(owner);
        vault.withdrawPool(owner, pool + 1);
        vm.prank(owner);
        vault.withdrawPool(owner, pool);
        assertEq(vault.poolBalance(), 0);
    }

    function test_Withdraw_CannotTouchEscrowOrHeld() public {
        _drainPool();
        _lock(ID1, 60 * UNIT);
        _lockComplete(ID2, 10 * UNIT);
        assertEq(_bal(address(vault)), 70 * UNIT); // token balance covers escrow + held
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, 1));
        vm.prank(owner);
        vault.withdrawPool(owner, 1);
        assertEq(vault.escrowedTotal(), 60 * UNIT);
        assertEq(vault.heldTotal(), 10 * UNIT);
    }

    function test_Withdraw_InvalidRecipientReverts() public {
        vm.startPrank(owner);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidRecipient.selector, address(0)));
        vault.withdrawPool(address(0), UNIT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidRecipient.selector, address(vault)));
        vault.withdrawPool(address(vault), UNIT);
        vm.stopPrank();
    }

    function test_Withdraw_AllowedWhilePaused() public {
        _pause();
        vm.prank(owner);
        vault.withdrawPool(owner, UNIT);
        assertEq(_bal(owner), UNIT);
    }

    // ------------------------------------------------------------ held funds are not movable
    function test_Held_EmitPaymentSelectorNoLongerExists() public {
        _lockComplete(ID1, AMT);
        uint256 vaultBal = _bal(address(vault));
        bytes memory data = abi.encodeWithSignature("emitPayment(address,uint256)", wallet, UNIT);
        address[2] memory callers = [settler, owner];
        for (uint256 i; i < callers.length; i++) {
            vm.prank(callers[i]);
            (bool ok,) = address(vault).call(data);
            assertFalse(ok, "emitPayment must not exist");
        }
        assertEq(_bal(address(vault)), vaultBal);
        assertEq(_bal(wallet), 0);
    }

    function test_Held_WithdrawPoolRevertsAboveAccountedPoolEvenIfTokenBalanceCoversHeld() public {
        _drainPool();
        _lockComplete(ID1, AMT);
        assertEq(vault.poolBalance(), 0);
        assertEq(_bal(address(vault)), AMT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, AMT));
        vm.prank(owner);
        vault.withdrawPool(owner, AMT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, 1));
        vm.prank(owner);
        vault.withdrawPool(owner, 1);
        assertEq(_bal(address(vault)), AMT);
    }

    function test_Held_SweepSurplusIsNoOpWhileFundsHeldOrEscrowed() public {
        _lock(ID1, AMT);
        _lockComplete(ID2, AMT);
        uint256 vaultBal = _bal(address(vault));
        uint256 ownerBal = _bal(owner);
        uint256 e = vault.escrowedTotal();
        uint256 h = vault.heldTotal();
        uint256 p = vault.poolBalance();
        vm.recordLogs();
        vm.prank(owner);
        vault.sweepSurplus(owner);
        assertEq(vm.getRecordedLogs().length, 0, "no event on no-op");
        assertEq(_bal(address(vault)), vaultBal);
        assertEq(_bal(owner), ownerBal);
        assertEq(vault.escrowedTotal(), e);
        assertEq(vault.heldTotal(), h);
        assertEq(vault.poolBalance(), p);
    }

    // ------------------------------------------------------------ sweepSurplus
    function test_Sweep_SendsExactlySurplusAndEmits() public {
        _lock(ID1, 5 * UNIT);
        _lockComplete(ID2, 6 * UNIT);
        vm.prank(funder);
        token.transfer(address(vault), 3 * UNIT + 7);
        uint256 e = vault.escrowedTotal();
        uint256 h = vault.heldTotal();
        uint256 p = vault.poolBalance();
        address to = makeAddr("sweepTo");

        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.SurplusSwept(to, 3 * UNIT + 7);
        vm.prank(owner);
        vault.sweepSurplus(to);

        assertEq(_bal(to), 3 * UNIT + 7);
        assertEq(vault.escrowedTotal(), e);
        assertEq(vault.heldTotal(), h);
        assertEq(vault.poolBalance(), p);
        assertEq(_bal(address(vault)), e + h + p);
        _assertAccounting();
    }

    function test_Sweep_SecondCallIsNoOp() public {
        vm.prank(funder);
        token.transfer(address(vault), 4 * UNIT);
        address to = makeAddr("sweepTo");
        vm.prank(owner);
        vault.sweepSurplus(to);
        assertEq(_bal(to), 4 * UNIT);
        vm.recordLogs();
        vm.prank(owner);
        vault.sweepSurplus(to);
        assertEq(vm.getRecordedLogs().length, 0);
        assertEq(_bal(to), 4 * UNIT);
    }

    function test_Sweep_OnlyOwner() public {
        vm.prank(funder);
        token.transfer(address(vault), UNIT);
        address[5] memory callers = [settler, rando, user, arbiter, guardian];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, callers[i]));
            vm.prank(callers[i]);
            vault.sweepSurplus(callers[i]);
        }
        assertEq(_bal(rando), 0);
    }

    function test_Sweep_InvalidRecipientReverts() public {
        vm.prank(funder);
        token.transfer(address(vault), UNIT);
        vm.startPrank(owner);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidRecipient.selector, address(0)));
        vault.sweepSurplus(address(0));
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidRecipient.selector, address(vault)));
        vault.sweepSurplus(address(vault));
        vm.stopPrank();
    }

    function test_Sweep_InvalidRecipientRevertsEvenWithoutSurplus() public {
        vm.startPrank(owner);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidRecipient.selector, address(0)));
        vault.sweepSurplus(address(0));
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidRecipient.selector, address(vault)));
        vault.sweepSurplus(address(vault));
        vm.stopPrank();
    }

    function test_Sweep_WorksWhilePaused() public {
        vm.prank(funder);
        token.transfer(address(vault), UNIT);
        _pause();
        vm.prank(owner);
        vault.sweepSurplus(owner);
        assertEq(_bal(owner), UNIT);
    }
}
