// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultBase} from "./ScanPayVaultBase.t.sol";

/// @dev onramp / emitPayment / daily cap / fundPool / withdrawPool unit tests.
contract ScanPayVaultPayoutTest is ScanPayVaultBase {
    bytes32 internal constant ORD1 = keccak256("order1");
    bytes32 internal constant ORD2 = keccak256("order2");

    function setUp() public override {
        super.setUp();
        _fund(2_000 * UNIT);
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
        address[3] memory callers = [owner, user, rando];
        for (uint256 i; i < 3; i++) {
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
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        vault.onramp(ORD1, address(0), UNIT, 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidAmount.selector, 0));
        vault.onramp(ORD1, wallet, 0, 1);
        uint256 pool = vault.poolBalance();
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, pool, pool + 1));
        vault.onramp(ORD1, wallet, pool + 1, 1);
        vm.stopPrank();
        assertFalse(vault.onrampDone(ORD1), "failed payout must not burn orderId");
    }

    function test_Onramp_PausedReverts() public {
        vm.prank(owner);
        vault.setPaused(true);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vm.prank(settler);
        vault.onramp(ORD1, wallet, UNIT, 1);
    }

    // ------------------------------------------------------------ emitPayment
    function test_EmitPayment_PaysWalletAndEmits() public {
        uint256 pool = vault.poolBalance();
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.PaymentEmitted(wallet, 7 * UNIT);
        vm.prank(settler);
        vault.emitPayment(wallet, 7 * UNIT);
        assertEq(_bal(wallet), 7 * UNIT);
        assertEq(vault.poolBalance(), pool - 7 * UNIT);
        assertEq(vault.emittedInWindow(), 7 * UNIT);
        _assertAccounting();
    }

    function test_EmitPayment_NoIdSoRepeatPaysTwice() public {
        vm.startPrank(settler);
        vault.emitPayment(wallet, UNIT);
        vault.emitPayment(wallet, UNIT);
        vm.stopPrank();
        assertEq(_bal(wallet), 2 * UNIT);
    }

    function test_EmitPayment_OnlySettler() public {
        address[3] memory callers = [owner, user, rando];
        for (uint256 i; i < 3; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedSettler.selector, callers[i]));
            vm.prank(callers[i]);
            vault.emitPayment(wallet, UNIT);
        }
    }

    function test_EmitPayment_InvalidInputsRevert() public {
        vm.startPrank(settler);
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        vault.emitPayment(address(0), UNIT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidAmount.selector, 0));
        vault.emitPayment(wallet, 0);
        uint256 pool = vault.poolBalance();
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, pool, pool + 1));
        vault.emitPayment(wallet, pool + 1);
        vm.stopPrank();
    }

    function test_EmitPayment_PausedReverts() public {
        vm.prank(owner);
        vault.setPaused(true);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vm.prank(settler);
        vault.emitPayment(wallet, UNIT);
    }

    function test_EmitPayment_CannotUseEscrow() public {
        // drain pool entirely, then lock escrow; payout must still fail
        uint256 pool = vault.poolBalance();
        vm.prank(owner);
        vault.withdrawPool(owner, pool);
        _lock(ID1, 50 * UNIT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, UNIT));
        vm.prank(settler);
        vault.emitPayment(wallet, UNIT);
        assertEq(vault.escrowedTotal(), 50 * UNIT);
    }

    // ------------------------------------------------------------ daily cap
    function test_Cap_SharedByOnrampAndEmit() public {
        assertEq(vault.remainingDailyCap(), 1000 * UNIT);
        vm.startPrank(settler);
        vault.onramp(ORD1, wallet, 600 * UNIT, 1);
        assertEq(vault.remainingDailyCap(), 400 * UNIT);
        vault.emitPayment(wallet, 400 * UNIT);
        assertEq(vault.remainingDailyCap(), 0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vault.emitPayment(wallet, 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vault.onramp(ORD2, wallet, 1, 1);
        vm.stopPrank();
        assertFalse(vault.onrampDone(ORD2));
    }

    function test_Cap_ExceedReportsRemaining() public {
        vm.prank(settler);
        vault.emitPayment(wallet, 990 * UNIT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 10 * UNIT, 10 * UNIT + 1));
        vm.prank(settler);
        vault.emitPayment(wallet, 10 * UNIT + 1);
        vm.prank(settler);
        vault.emitPayment(wallet, 10 * UNIT); // exactly remaining is fine
    }

    function test_Cap_FixedWindowBoundary() public {
        vm.prank(settler);
        vault.emitPayment(wallet, 1000 * UNIT);
        vm.warp(T0 + 1 days - 1);
        assertEq(vault.remainingDailyCap(), 0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vm.prank(settler);
        vault.emitPayment(wallet, 1);

        vm.warp(T0 + 1 days);
        assertEq(vault.remainingDailyCap(), 1000 * UNIT); // view shows full cap before the new window starts
        assertEq(vault.emittedInWindow(), 1000 * UNIT); // storage not rolled yet
        vm.prank(settler);
        vault.emitPayment(wallet, 300 * UNIT);
        assertEq(vault.emitWindowStart(), T0 + 1 days);
        assertEq(vault.emittedInWindow(), 300 * UNIT);
        assertEq(vault.remainingDailyCap(), 700 * UNIT);
    }

    function test_Cap_WindowIsFixedNotRolling() public {
        vm.warp(T0 + 23 hours);
        vm.prank(settler);
        vault.emitPayment(wallet, 500 * UNIT);
        assertEq(vault.emitWindowStart(), T0, "payout inside window must not move its start");
        vm.warp(T0 + 24 hours);
        assertEq(vault.remainingDailyCap(), 1000 * UNIT);
    }

    function test_Cap_LoweringBelowUsedGivesZeroRemaining() public {
        vm.prank(settler);
        vault.emitPayment(wallet, 500 * UNIT);
        vm.prank(owner);
        vault.setDailyEmitCap(100 * UNIT);
        assertEq(vault.remainingDailyCap(), 0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vm.prank(settler);
        vault.emitPayment(wallet, 1);
        vm.prank(owner);
        vault.setDailyEmitCap(600 * UNIT);
        assertEq(vault.remainingDailyCap(), 100 * UNIT);
    }

    function test_Cap_ZeroCapDisablesPayouts() public {
        vm.prank(owner);
        vault.setDailyEmitCap(0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vm.prank(settler);
        vault.emitPayment(wallet, 1);
    }

    function test_Cap_FailedPayoutDoesNotConsumeCap() public {
        uint256 pool = vault.poolBalance();
        vm.prank(settler);
        vault.emitPayment(wallet, 100 * UNIT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, pool - 100 * UNIT, pool));
        vm.prank(settler);
        vault.emitPayment(wallet, pool);
        assertEq(vault.emittedInWindow(), 100 * UNIT);
        assertEq(vault.remainingDailyCap(), 900 * UNIT);
    }

    function test_Cap_FailedPayoutInNewWindowDoesNotRollWindow() public {
        vm.prank(settler);
        vault.emitPayment(wallet, 100 * UNIT);
        vm.warp(T0 + 2 days);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 1000 * UNIT, 1001 * UNIT));
        vm.prank(settler);
        vault.emitPayment(wallet, 1001 * UNIT);
        assertEq(vault.emitWindowStart(), T0);
        assertEq(vault.emittedInWindow(), 100 * UNIT);
    }

    // ------------------------------------------------------------ fundPool
    function test_FundPool_AnyoneAnyTimeEvent() public {
        uint256 pool = vault.poolBalance();
        _mint(rando, 9 * UNIT);
        _approve(rando, address(vault), 9 * UNIT);
        vm.prank(owner);
        vault.setPaused(true); // funding allowed while paused
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
        address[3] memory callers = [settler, rando, user];
        for (uint256 i; i < 3; i++) {
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
        vault.withdrawPool(owner, pool); // exactly the pool is fine
        assertEq(vault.poolBalance(), 0);
    }

    function test_Withdraw_CannotTouchEscrow() public {
        uint256 pool = vault.poolBalance();
        vm.prank(owner);
        vault.withdrawPool(owner, pool);
        _lock(ID1, 60 * UNIT);
        assertEq(_bal(address(vault)), 60 * UNIT); // token balance covers the escrow
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, 1));
        vm.prank(owner);
        vault.withdrawPool(owner, 1);
        assertEq(vault.escrowedTotal(), 60 * UNIT);
    }

    function test_Withdraw_ZeroAddressReverts() public {
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        vm.prank(owner);
        vault.withdrawPool(address(0), UNIT);
    }

    function test_Withdraw_AllowedWhilePaused() public {
        vm.startPrank(owner);
        vault.setPaused(true);
        vault.withdrawPool(owner, UNIT);
        vm.stopPrank();
        assertEq(_bal(owner), UNIT);
    }
}
