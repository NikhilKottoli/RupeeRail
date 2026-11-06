// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultFuzzBase} from "./ScanPayVaultFuzzBase.t.sol";

/// @dev Fuzz tests over the quote (commitment, inrPaise, rateE6), signature integrity,
/// the completion window and the completeDelay / reclaimDelay gap relation.
contract ScanPayVaultFuzzQuoteTest is ScanPayVaultFuzzBase {
    // ------------------------------------------------------------ qrCommitment
    function testFuzz_NonZeroCommitmentRoundTripsToStorageAndEvent(bytes32 c) public {
        vm.assume(c != bytes32(0));
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        it.qrCommitment = c;
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpLocked(ID1, user, UNIT, it.inrPaise, it.rateE6, c);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
        assertEq(_off(ID1).qrCommitment, c);
    }

    function test_ZeroCommitmentRevertsInvalidQuote() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        it.qrCommitment = bytes32(0);
        _tryOfframp(it, ScanPayVault.InvalidQuote.selector, "");
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
        assertEq(vault.escrowedTotal(), 0);
    }

    function testFuzz_CommitmentFromSaltAndTextLocks(bytes32 salt, string memory text) public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        it.qrCommitment = _commit(salt, text);
        _tryOfframp(it, bytes4(0), "");
        assertEq(_off(ID1).qrCommitment, keccak256(abi.encode(salt, text)));
    }

    // ------------------------------------------------------------ inrPaise / rateE6
    function testFuzz_InrPaiseAndRateZeroRevertsOtherwiseLocks(uint64 inr, uint64 rate) public {
        // bias towards the zero edge cases
        if (uint256(keccak256(abi.encode(inr))) % 4 == 0) inr = 0;
        if (uint256(keccak256(abi.encode(rate))) % 4 == 0) rate = 0;
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        it.inrPaise = inr;
        it.rateE6 = rate;
        if (inr == 0 || rate == 0) {
            _tryOfframp(it, ScanPayVault.InvalidQuote.selector, "");
            assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
        } else {
            _tryOfframp(it, bytes4(0), "");
            assertEq(_off(ID1).inrPaise, inr);
            skip(15);
            vm.expectEmit(true, false, false, true, address(vault));
            emit ScanPayVault.OfframpCompleted(ID1, inr, REF);
            vm.prank(settler);
            vault.completeOfframp(ID1, REF);
        }
    }

    function testFuzz_QuoteSignedForOtherFieldsIsRejected(uint64 inr, uint64 rate) public {
        inr = uint64(bound(inr, 1, type(uint64).max));
        rate = uint64(bound(rate, 1, type(uint64).max));
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        // quote signer signed a different (inrPaise, rateE6) pair than the one the user signed
        vm.assume(inr != it.inrPaise || rate != it.rateE6);
        ScanPayVault.OfframpIntent memory other = _intent(ID1, user, UNIT, QR); // fresh copy, not an alias
        other.inrPaise = inr;
        other.rateE6 = rate;
        bytes memory qsig = _signQuote(vault, quotePk, other);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), UNIT, block.timestamp + 1 hours);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vault.offramp(it, sig, qsig, ps);
    }

    // ------------------------------------------------------------ signature byte flips
    function testFuzz_FlippingAnyQuoteSigByteReverts(uint256 idx, uint8 mask) public {
        idx = bound(idx, 0, 64);
        mask = uint8(bound(mask, 1, 255));
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        assertEq(qsig.length, 65);
        qsig[idx] = bytes1(uint8(qsig[idx]) ^ mask);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vault.offramp(it, sig, qsig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
    }

    function testFuzz_FlippingAnyUserSigByteReverts(uint256 idx, uint8 mask) public {
        idx = bound(idx, 0, 64);
        mask = uint8(bound(mask, 1, 255));
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        assertEq(sig.length, 65);
        sig[idx] = bytes1(uint8(sig[idx]) ^ mask);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, qsig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
    }

    function testFuzz_SignatureBindsEveryCommitmentBit(bytes32 c, uint256 bit) public {
        vm.assume(c != bytes32(0));
        bit = bound(bit, 0, 255);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        it.qrCommitment = c;
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        bytes32 flipped = c ^ bytes32(uint256(1) << bit);
        vm.assume(flipped != bytes32(0));
        it.qrCommitment = flipped;
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, qsig, ps);
    }

    // ------------------------------------------------------------ completion window
    function _setDelays(uint256 cd, uint256 rd) internal {
        vm.startPrank(owner);
        vault.setReclaimDelay(rd); // rd >= 2h so the default 15s completeDelay satisfies the gap
        vault.setCompleteDelay(cd); // cd + 1h <= rd by construction
        vm.stopPrank();
    }

    /// @dev lockedAt == T0 (setUp warp); complete after `dt` seconds. Succeeds iff cd <= dt < rd.
    function _checkComplete(uint256 cd, uint256 rd, uint256 dt) internal {
        _setDelays(cd, rd);
        _lock(ID1, UNIT);
        assertEq(_dl(ID1).completableAt, T0 + cd);
        assertEq(_dl(ID1).reclaimableAt, T0 + rd);
        skip(dt);
        if (dt < cd) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, T0 + cd));
        } else if (dt >= rd) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.CompletionWindowClosed.selector, T0 + rd));
        }
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        if (dt >= cd && dt < rd) {
            assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));
            assertEq(vault.heldTotal(), UNIT);
        } else {
            assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
            assertEq(vault.escrowedTotal(), UNIT);
        }
        if (dt >= rd) {
            // a window that has closed leaves reclaim as the way out, open to anyone
            vm.prank(rando);
            vault.reclaimOfframp(ID1);
            assertEq(_bal(user), 10_000 * UNIT);
        }
        _assertAccounting();
    }

    function testFuzz_CompletionWindowAnyOffset(uint256 cd, uint256 rd, uint256 dt) public {
        rd = bound(rd, 2 hours, 30 days);
        cd = bound(cd, 0, rd - 1 hours > 1 days ? 1 days : rd - 1 hours);
        dt = bound(dt, 0, rd + 2 hours);
        _checkComplete(cd, rd, dt);
    }

    function testFuzz_CompletionWindowEdges(uint256 cd, uint256 rd, bool atReclaim, uint8 off) public {
        rd = bound(rd, 2 hours, 30 days);
        cd = bound(cd, 0, rd - 1 hours > 1 days ? 1 days : rd - 1 hours);
        uint256 anchor = atReclaim ? rd : cd;
        off = uint8(bound(off, 0, 4)); // anchor-2 .. anchor+2
        uint256 dt = anchor + off < 2 ? 0 : anchor + off - 2;
        _checkComplete(cd, rd, dt);
    }

    // ------------------------------------------------------------ delay-gap relation
    function testFuzz_SetCompleteDelayAcceptedIffWithinBoundsAndGap(uint256 cd) public {
        cd = bound(cd, 0, 3 days);
        uint256 rd = vault.reclaimDelay();
        if (cd > 1 days) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidCompleteDelay.selector, cd));
        } else if (cd + 1 hours > rd) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidDelayGap.selector, cd, rd));
        }
        vm.prank(owner);
        vault.setCompleteDelay(cd);
        bool ok = cd <= 1 days && cd + 1 hours <= rd;
        assertEq(vault.completeDelay(), ok ? cd : 15);
    }

    function testFuzz_SetReclaimDelayAcceptedIffWithinBoundsAndGap(uint256 rd) public {
        rd = bound(rd, 0, 40 days);
        uint256 cd = vault.completeDelay();
        if (rd < 1 hours || rd > 30 days) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidReclaimDelay.selector, rd));
        } else if (cd + 1 hours > rd) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidDelayGap.selector, cd, rd));
        }
        vm.prank(owner);
        vault.setReclaimDelay(rd);
        bool ok = rd >= 1 hours && rd <= 30 days && cd + 1 hours <= rd;
        assertEq(vault.reclaimDelay(), ok ? rd : 24 hours);
    }

    function testFuzz_DelayPairReachableIffGapAndBoundsHold(uint256 cd, uint256 rd) public {
        cd = bound(cd, 0, 2 days);
        rd = bound(rd, 0, 40 days);
        bool valid = cd <= 1 days && rd >= 1 hours && rd <= 30 days && cd + 1 hours <= rd;
        vm.startPrank(owner);
        if (valid) {
            // order matters: widen first (rd up) or tighten first (cd down)
            if (rd >= 24 hours) {
                vault.setReclaimDelay(rd);
                vault.setCompleteDelay(cd);
            } else {
                vault.setCompleteDelay(cd);
                vault.setReclaimDelay(rd);
            }
            assertEq(vault.completeDelay(), cd);
            assertEq(vault.reclaimDelay(), rd);
        } else {
            // no ordering of the setters can reach an invalid pair
            try vault.setReclaimDelay(rd) {} catch {}
            try vault.setCompleteDelay(cd) {} catch {}
            try vault.setReclaimDelay(rd) {} catch {}
            try vault.setCompleteDelay(cd) {} catch {}
            assertTrue(!(vault.completeDelay() == cd && vault.reclaimDelay() == rd));
        }
        vm.stopPrank();
        assertLe(vault.completeDelay() + 1 hours, vault.reclaimDelay());
    }
}
