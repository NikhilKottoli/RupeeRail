// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultBase} from "./ScanPayVaultBase.t.sol";

/// @dev Fuzz tests over amounts, qrText, timing and payout limits.
contract ScanPayVaultFuzzTest is ScanPayVaultBase {
    uint256 internal constant U96 = type(uint96).max;

    function _tryOfframp(ScanPayVault.OfframpIntent memory it, bytes4 expectedErr, bytes memory errArgs) internal {
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        if (expectedErr != bytes4(0)) vm.expectRevert(bytes.concat(expectedErr, errArgs));
        vm.prank(relayer);
        vault.offramp(it, sig, ps);
    }

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
        vault.setMaxOfframpAmount(U96);
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

    function testFuzz_LargeAmountRoundTripsInStruct(uint256 amount) public {
        amount = bound(amount, 1, U96);
        vm.prank(owner);
        vault.setMaxOfframpAmount(U96);
        _mint(user, amount);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amount, QR);
        _tryOfframp(it, bytes4(0), "");
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

    // ------------------------------------------------------------ qrText
    function testFuzz_QrLengthValidIff1To512(uint256 len) public {
        len = bound(len, 0, 600);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, _qr(len));
        if (len == 0 || len > 512) {
            _tryOfframp(it, ScanPayVault.InvalidQrLength.selector, abi.encode(len));
            assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
        } else {
            _tryOfframp(it, bytes4(0), "");
            assertEq(_off(ID1).qrHash, keccak256(bytes(_qr(len))));
        }
    }

    function _normalize(bytes memory raw) internal pure returns (string memory) {
        uint256 n = raw.length;
        if (n == 0) return string(new bytes(1)); // a single NUL byte
        if (n > 512) n = 512;
        bytes memory b = new bytes(n);
        for (uint256 i; i < n; i++) b[i] = raw[i];
        return string(b);
    }

    function testFuzz_ArbitraryQrBytesRoundTripInEventAndHash(bytes memory raw) public {
        string memory qr = _normalize(raw);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, qr);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpLocked(ID1, user, UNIT, it.inrPaise, it.rateE6, qr);
        vm.prank(relayer);
        vault.offramp(it, sig, ps);
        assertEq(_off(ID1).qrHash, keccak256(bytes(qr)));
    }

    function testFuzz_SignatureBindsEveryQrByte(bytes memory raw, uint256 idx) public {
        string memory qr = _normalize(raw);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, qr);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        bytes memory tampered = bytes(qr);
        idx = bound(idx, 0, tampered.length - 1);
        tampered[idx] = bytes1(uint8(tampered[idx]) ^ 0x01);
        it.qrText = string(tampered);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, ps);
    }

    function test_Qr_MultibyteAndNulBytesRoundTrip() public {
        string[3] memory qrs = [unicode"upi://pay?pn=₹ 日本語 🚀", string(abi.encodePacked("a", bytes1(0x00), "b")), string(hex"00")];
        for (uint256 i; i < qrs.length; i++) {
            bytes32 id = bytes32(i + 1);
            ScanPayVault.OfframpIntent memory it = _intent(id, user, UNIT, qrs[i]);
            (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
            vm.expectEmit(true, true, false, true, address(vault));
            emit ScanPayVault.OfframpLocked(id, user, UNIT, it.inrPaise, it.rateE6, qrs[i]);
            vault.offramp(it, sig, ps);
            assertEq(_off(id).qrHash, keccak256(bytes(qrs[i])));
        }
    }

    // ------------------------------------------------------------ timing
    function testFuzz_CompleteTiming(uint256 delay, uint256 dt) public {
        delay = bound(delay, 0, 1 days);
        dt = bound(dt, 0, 2 days);
        vm.prank(owner);
        vault.setCompleteDelay(delay);
        _lock(ID1, UNIT);
        skip(dt);
        if (dt < delay) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + delay));
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        if (dt >= delay) assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));
        else assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
    }

    function testFuzz_ReclaimTiming(uint256 delay, uint256 dt) public {
        delay = bound(delay, 1 hours, 30 days);
        dt = bound(dt, 0, delay + 2 hours);
        vm.prank(owner);
        vault.setReclaimDelay(delay);
        _lock(ID1, UNIT);
        skip(dt);
        if (dt < delay) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + delay));
        vm.prank(user);
        vault.reclaimOfframp(ID1);
        if (dt >= delay) {
            assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REFUNDED));
            assertEq(_bal(user), 10_000 * UNIT);
        }
        _assertAccounting();
    }

    function testFuzz_ReverseTiming(uint256 window, uint256 dt) public {
        window = bound(window, 0, 30 days);
        dt = bound(dt, 0, window + 2 hours);
        vm.prank(owner);
        vault.setDisputeWindow(window);
        _lockComplete(ID1, UNIT);
        uint256 tc = _completedAt(ID1);
        vm.warp(tc + dt);
        if (dt > window) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, tc + window));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        if (dt <= window) assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REVERSED));
        _assertAccounting();
    }

    function testFuzz_SnapshotsIgnoreLaterSetterChanges(uint256 newReclaim, uint256 newWindow, uint256 dt) public {
        newReclaim = bound(newReclaim, 1 hours, 30 days);
        newWindow = bound(newWindow, 0, 30 days);
        dt = bound(dt, 0, 60 days);
        _lock(ID1, UNIT);
        vm.startPrank(owner);
        vault.setReclaimDelay(newReclaim);
        vault.setDisputeWindow(newWindow);
        vm.stopPrank();
        assertEq(_dl(ID1).reclaimableAt, T0 + 24 hours);
        assertEq(_dl(ID1).disputeWindow, 24 hours);
        skip(dt);
        if (dt < 24 hours) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + 24 hours));
        vm.prank(user);
        vault.reclaimOfframp(ID1);
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

    function testFuzz_CumulativePaymentsNeverExceedCapInWindow(uint256 a, uint256 b, uint256 c, uint256 d) public {
        _fund(3000 * UNIT);
        uint256[4] memory xs = [a, b, c, d];
        uint256 paid;
        for (uint256 i; i < 4; i++) {
            uint256 x = bound(xs[i], 1, 600 * UNIT);
            vm.prank(settler);
            try vault.emitPayment(wallet, x) {
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
        vault.emitPayment(wallet, 1000 * UNIT);
        vm.warp(T0 + dt);
        if (dt < 1 days) vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DailyEmitCapExceeded.selector, 0, 1));
        vm.prank(settler);
        vault.emitPayment(wallet, 1);
        if (dt >= 1 days) {
            assertEq(vault.emitWindowStart(), T0 + dt);
            assertEq(vault.emittedInWindow(), 1);
        }
    }

    // ------------------------------------------------------------ accounting
    function testFuzz_SingleOfframpBalanceEqualsEscrowPlusPool(uint256 amount, uint256 poolFund, uint8 action) public {
        amount = bound(amount, 1, 100 * UNIT);
        poolFund = bound(poolFund, 0, 500 * UNIT);
        action = uint8(bound(action, 0, 4));
        if (poolFund > 0) _fund(poolFund);
        _lock(ID1, amount);
        _assertAccounting();
        skip(15);
        vm.startPrank(settler);
        if (action == 1) vault.completeOfframp(ID1, REF);
        if (action == 2) vault.refundOfframp(ID1, 1);
        if (action == 3) {
            vault.completeOfframp(ID1, REF);
            vault.reverseOfframp(ID1);
        }
        vm.stopPrank();
        if (action == 4) {
            skip(24 hours);
            vm.prank(user);
            vault.reclaimOfframp(ID1);
        }
        assertEq(_bal(address(vault)), vault.escrowedTotal() + vault.poolBalance());
        uint256 expectEscrow = action == 0 ? amount : 0;
        assertEq(vault.escrowedTotal(), expectEscrow);
        assertEq(vault.poolBalance(), poolFund + (action == 1 ? amount : 0));
        assertEq(_bal(settler), 0);
        assertEq(_bal(relayer), 0);
    }
}
