// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultFuzzBase} from "./ScanPayVaultFuzzBase.t.sol";

/// @dev Fuzz tests over amounts, timing, payouts vs pool/cap, accounting and surplus.
contract ScanPayVaultFuzzTest is ScanPayVaultFuzzBase {
    // ------------------------------------------------------------ amounts
    function testFuzz_AmountAgainstDefaultMax(uint256 amount) public {
        amount = bound(amount, 0, 300 * UNIT);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amount, QR);
        if (amount == 0) {
            _tryOfframp(it, ScanPayVault.InvalidAmount.selector, abi.encode(amount));
        } else if (amount > 100 * UNIT) {
            _tryOfframp(it, ScanPayVault.OfframpAmountTooLarge.selector, abi.encode(amount));
        } else {
            _tryOfframp(it, bytes4(0), "");
            assertEq(_off(ID1).amount, amount);
            assertEq(vault.escrowedTotal(), amount);
        }
        _assertAccounting();
    }

    function testFuzz_AmountAroundUint96Boundary(uint256 offset) public {
        offset = bound(offset, 0, 3);
        uint256 amount = U96 - 1 + offset; // U96-1, U96, U96+1, U96+2
        vm.prank(owner);
        vault.setLimits(U96, 1000 * UNIT);
        _mint(user, amount);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amount, QR);
        if (amount > U96) {
            _tryOfframp(it, ScanPayVault.OfframpAmountTooLarge.selector, abi.encode(amount));
            assertEq(vault.escrowedTotal(), 0);
        } else {
            _tryOfframp(it, bytes4(0), "");
            assertEq(_off(ID1).amount, amount);
            assertEq(vault.escrowedTotal(), amount);
        }
    }

    function testFuzz_SetLimitsAcceptsMaxIffFitsUint96(uint256 max, uint256 cap) public {
        max = bound(max, U96 - 3, U96 + 3);
        if (max > U96) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, max));
        vm.prank(owner);
        vault.setLimits(max, cap);
        if (max <= U96) {
            assertEq(vault.maxOfframpAmount(), max);
            assertEq(vault.dailyEmitCap(), cap);
        } else {
            assertEq(vault.maxOfframpAmount(), 100 * UNIT);
        }
    }

    function testFuzz_LargeAmountRoundTripsInStruct(uint256 amount) public {
        amount = bound(amount, 1, U96);
        vm.prank(owner);
        vault.setLimits(U96, 1000 * UNIT);
        _mint(user, amount);
        _tryOfframp(_intent(ID1, user, amount, QR), bytes4(0), "");
        assertEq(uint256(_off(ID1).amount), amount);
        assertEq(vault.escrowedTotal(), amount);
        assertEq(_bal(address(vault)), amount);
    }

    function testFuzz_AmountAtAndAboveConfiguredMax(uint256 max) public {
        max = bound(max, 1, U96);
        vm.prank(owner);
        vault.setMaxOfframpAmount(max);
        _mint(user, max + 1);
        _tryOfframp(_intent(ID1, user, max, QR), bytes4(0), "");
        _tryOfframp(_intent(ID2, user, max + 1, QR), ScanPayVault.OfframpAmountTooLarge.selector, abi.encode(max + 1));
        assertEq(vault.escrowedTotal(), max);
    }

    // ------------------------------------------------------------ timing
    function testFuzz_ReclaimTiming(uint256 delay, uint256 dt, bool byUser) public {
        delay = bound(delay, 1 hours + 15, 30 days);
        dt = bound(dt, 0, delay + 2 hours);
        vm.prank(owner);
        vault.setReclaimDelay(delay);
        _lock(ID1, UNIT);
        skip(dt);
        if (dt < delay) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + delay));
        vm.prank(byUser ? user : rando); // anyone may reclaim
        vault.reclaimOfframp(ID1);
        if (dt >= delay) {
            assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REFUNDED));
            assertEq(_bal(user), 10_000 * UNIT);
        } else {
            assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
        }
        _assertAccounting();
    }

    /// @dev Sets the dispute window, then lock + complete ID1; returns completion time.
    function _completedWithWindow(uint256 window) internal returns (uint256 tc) {
        vm.prank(owner);
        vault.setDisputeWindow(window);
        _lockComplete(ID1, UNIT);
        tc = _completedAt(ID1);
        assertEq(_dl(ID1).releasableAt, tc + window);
    }

    function testFuzz_ReverseTiming(uint256 window, uint256 dt) public {
        window = bound(window, 1 hours, 30 days);
        dt = bound(dt, 0, window + 2 hours);
        uint256 tc = _completedWithWindow(window);
        vm.warp(tc + dt);
        if (dt >= window) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, tc + window));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        if (dt < window) assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REVERSED));
        else assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));
        _assertAccounting();
    }

    function testFuzz_ReleaseTiming(uint256 window, uint256 dt) public {
        window = bound(window, 1 hours, 30 days);
        dt = bound(dt, 0, window + 2 hours);
        uint256 tc = _completedWithWindow(window);
        vm.warp(tc + dt);
        if (dt < window) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, tc + window));
        vm.prank(rando);
        vault.releaseOfframp(ID1);
        if (dt >= window) {
            assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.RELEASED));
            assertEq(vault.poolBalance(), UNIT);
            assertEq(vault.heldTotal(), 0);
        } else {
            assertEq(vault.poolBalance(), 0);
            assertEq(vault.heldTotal(), UNIT);
        }
        _assertAccounting();
    }

    function testFuzz_DisputeTiming(uint256 window, uint256 dt) public {
        window = bound(window, 1 hours, 30 days);
        dt = bound(dt, 0, window + 2 hours);
        uint256 tc = _completedWithWindow(window);
        vm.warp(tc + dt);
        if (dt >= window) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, tc + window));
        vm.prank(user);
        vault.disputeOfframp(ID1);
        if (dt < window) assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.DISPUTED));
        else assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));
    }

    function testFuzz_SnapshotsIgnoreLaterSetterChanges(uint256 newReclaim, uint256 newWindow, uint256 dt) public {
        newReclaim = bound(newReclaim, 1 hours + 15, 30 days);
        newWindow = bound(newWindow, 1 hours, 30 days);
        dt = bound(dt, 0, 60 days);
        _lock(ID1, UNIT);
        vm.startPrank(owner);
        vault.setReclaimDelay(newReclaim);
        vault.setDisputeWindow(newWindow);
        vm.stopPrank();
        assertEq(_dl(ID1).completableAt, T0 + 15);
        assertEq(_dl(ID1).reclaimableAt, T0 + 24 hours);
        assertEq(_dl(ID1).disputeWindow, 24 hours);
        skip(dt);
        if (dt < 24 hours) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + 24 hours));
        vm.prank(user);
        vault.reclaimOfframp(ID1);
    }

    function testFuzz_CompletionUsesLockTimeDisputeWindow(uint256 newWindow) public {
        newWindow = bound(newWindow, 1 hours, 30 days);
        _lock(ID1, UNIT);
        vm.prank(owner);
        vault.setDisputeWindow(newWindow);
        skip(15);
        _complete(ID1);
        assertEq(_dl(ID1).releasableAt, block.timestamp + 24 hours);
    }

    // ------------------------------------------------------------ payouts vs pool and cap
    function testFuzz_PayoutAgainstPoolAndCap(uint256 pool, uint256 amount, uint256 cap) public {
        pool = bound(pool, 1, 5000 * UNIT);
        amount = bound(amount, 0, 6000 * UNIT);
        cap = bound(cap, 0, 3000 * UNIT);
        _fund(pool);
        vm.prank(owner);
        vault.setDailyEmitCap(cap);
        if (amount == 0) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidAmount.selector, 0));
        } else if (amount > pool) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, pool, amount));
        } else if (amount > cap) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, cap, amount));
        }
        vm.prank(settler);
        vault.onramp(ID1, wallet, amount, 1);
        bool ok = amount != 0 && amount <= pool && amount <= cap;
        assertEq(_bal(wallet), ok ? amount : 0);
        assertEq(vault.poolBalance(), ok ? pool - amount : pool);
        assertEq(vault.onrampDone(ID1), ok);
        assertEq(vault.remainingDailyCap(), ok ? cap - amount : cap);
        _assertAccounting();
    }

    function testFuzz_OnrampCannotTouchEscrowOrHeld(uint256 amount, uint256 pool) public {
        pool = bound(pool, 0, 200 * UNIT);
        amount = bound(amount, 1, 600 * UNIT);
        if (pool > 0) _fund(pool);
        _lockComplete(ID1, 40 * UNIT); // held
        _lock(ID2, 30 * UNIT); // escrowed
        if (amount > pool) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, pool, amount));
        }
        vm.prank(settler);
        vault.onramp(ID3, wallet, amount, 1);
        assertEq(vault.heldTotal(), 40 * UNIT);
        assertEq(vault.escrowedTotal(), 30 * UNIT);
        assertGe(_bal(address(vault)), 70 * UNIT);
        _assertAccounting();
    }

    function testFuzz_CumulativePaymentsNeverExceedCapInWindow(uint256 a, uint256 b, uint256 c, uint256 d) public {
        _fund(3000 * UNIT);
        uint256[4] memory xs = [a, b, c, d];
        uint256 paid;
        for (uint256 i; i < 4; i++) {
            uint256 x = bound(xs[i], 1, 600 * UNIT);
            vm.prank(settler);
            try vault.onramp(keccak256(abi.encode("order", i)), wallet, x, 1) {
                paid += x;
            } catch {}
            assertLe(paid, 1000 * UNIT);
            assertEq(vault.emittedInWindow(), paid);
        }
        assertEq(_bal(wallet), paid);
        assertEq(vault.poolBalance(), 3000 * UNIT - paid);
        _assertAccounting();
    }

    function testFuzz_CapWindowResetsExactlyAfterOneDay(uint256 dt) public {
        dt = bound(dt, 0, 3 days);
        _fund(3000 * UNIT);
        vm.prank(settler);
        vault.onramp(ID1, wallet, 1000 * UNIT, 1);
        vm.warp(T0 + dt);
        if (dt < 1 days) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vm.prank(settler);
        vault.onramp(ID2, wallet, 1, 1);
        if (dt >= 1 days) {
            assertEq(vault.emitWindowStart(), T0 + dt);
            assertEq(vault.emittedInWindow(), 1);
        }
    }

    // ------------------------------------------------------------ accounting
    function testFuzz_SingleOfframpBalanceEqualsEscrowHeldPool(uint256 amount, uint256 poolFund, uint8 action) public {
        amount = bound(amount, 1, 100 * UNIT);
        poolFund = bound(poolFund, 0, 500 * UNIT);
        action = uint8(bound(action, 0, 5));
        if (poolFund > 0) _fund(poolFund);
        _lock(ID1, amount);
        _assertAccounting();
        skip(15);
        vm.startPrank(settler);
        if (action == 1 || action == 3 || action == 5) vault.completeOfframp(ID1, REF);
        if (action == 2) vault.refundOfframp(ID1, 1);
        if (action == 3) vault.reverseOfframp(ID1);
        vm.stopPrank();
        if (action == 4) {
            skip(24 hours);
            vm.prank(user);
            vault.reclaimOfframp(ID1);
        }
        if (action == 5) {
            vm.warp(_dl(ID1).releasableAt);
            vault.releaseOfframp(ID1);
        }
        assertEq(_bal(address(vault)), vault.escrowedTotal() + vault.heldTotal() + vault.poolBalance());
        assertEq(vault.escrowedTotal(), action == 0 ? amount : 0);
        assertEq(vault.heldTotal(), action == 1 ? amount : 0);
        assertEq(vault.poolBalance(), poolFund + (action == 5 ? amount : 0));
        assertEq(_bal(settler), 0);
        assertEq(_bal(relayer), 0);
    }

    // ------------------------------------------------------------ surplus
    function testFuzz_SweepSurplusSendsExactlySurplus(uint256 donation, uint256 a1, uint256 a2, uint256 poolFund)
        public
    {
        donation = bound(donation, 0, 1000 * UNIT);
        a1 = bound(a1, 1, 100 * UNIT);
        a2 = bound(a2, 1, 100 * UNIT);
        poolFund = bound(poolFund, 0, 500 * UNIT);
        if (poolFund > 0) _fund(poolFund);
        _lockComplete(ID2, a2); // held
        _lock(ID1, a1); // escrowed
        if (donation > 0) {
            vm.prank(funder);
            token.transfer(address(vault), donation);
        }
        uint256 esc = vault.escrowedTotal();
        uint256 held = vault.heldTotal();
        uint256 pool = vault.poolBalance();
        assertEq(_bal(address(vault)), esc + held + pool + donation);

        address sink = makeAddr("sink");
        if (donation > 0) {
            vm.expectEmit(true, false, false, true, address(vault));
            emit ScanPayVault.SurplusSwept(sink, donation);
        }
        vm.prank(owner);
        vault.sweepSurplus(sink);

        assertEq(_bal(sink), donation);
        assertEq(vault.escrowedTotal(), esc);
        assertEq(vault.heldTotal(), held);
        assertEq(vault.poolBalance(), pool);
        assertEq(_bal(address(vault)), esc + held + pool);
        // a second sweep has nothing left to send
        vm.prank(owner);
        vault.sweepSurplus(sink);
        assertEq(_bal(sink), donation);
        assertEq(_bal(address(vault)), esc + held + pool);
    }
}
