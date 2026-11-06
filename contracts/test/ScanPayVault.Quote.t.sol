// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Vm} from "forge-std/Vm.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultUnitBase} from "./ScanPayVaultUnitBase.t.sol";

/// @dev Quote-signature authentication, qrCommitment privacy, id-squatting and gasless relay tests.
contract ScanPayVaultQuoteTest is ScanPayVaultUnitBase {
    uint256 internal constant SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;

    function _ps(ScanPayVault.OfframpIntent memory it) internal view returns (ScanPayVault.PermitSig memory) {
        return _signPermit(tokAddr, userPk, address(vault), it.amount, block.timestamp + 1 hours);
    }

    // ------------------------------------------------------------ quote signature required
    function test_Quote_EmptyQuoteSigReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _ps(it);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, "", ps);
        assertEq(_st(ID1), uint8(NONE));
        assertEq(vault.escrowedTotal(), 0);
    }

    function test_Quote_SignedByUsersOwnKeyReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _ps(it);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(user);
        vault.offramp(it, sig, sig, ps); // same signature reused as the quote
        assertEq(_st(ID1), uint8(NONE));
    }

    function test_Quote_AttackOneBaseUnitAt200000PaiseSignedOnlyByUserReverts() public {
        (, uint256 randomPk) = makeAddrAndKey("random-quote-key");
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 1, QR);
        it.inrPaise = 200_000;
        bytes memory sig = _signIntent(vault, userPk, it);
        bytes memory fakeQuote = _signQuote(vault, randomPk, it);
        ScanPayVault.PermitSig memory ps = _ps(it);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(user);
        vault.offramp(it, sig, fakeQuote, ps);
        assertEq(_st(ID1), uint8(NONE));
        assertEq(vault.escrowedTotal(), 0);
        assertEq(_bal(address(vault)), 0);
    }

    function test_Quote_ValidQuoteMakesSameAttackShapeSucceed() public {
        // positive control: the same shape with a genuine quote signature locks fine
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 1, QR);
        it.inrPaise = 200_000;
        _relay(it, userPk, userPk);
        assertEq(_st(ID1), uint8(LOCKED));
        assertEq(_off(ID1).inrPaise, 200_000);
    }

    function test_Quote_ZeroInrPaiseReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        it.inrPaise = 0;
        _expectInvalidQuoteWithValidSigs(it);
    }

    function test_Quote_ZeroRateReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        it.rateE6 = 0;
        _expectInvalidQuoteWithValidSigs(it);
    }

    function test_Quote_ZeroQrCommitmentReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        it.qrCommitment = bytes32(0);
        _expectInvalidQuoteWithValidSigs(it);
    }

    function _expectInvalidQuoteWithValidSigs(ScanPayVault.OfframpIntent memory it) internal {
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
        assertEq(_st(it.id), uint8(NONE));
    }

    function test_Quote_ZeroFieldsRevertBeforeSignaturesAreExamined() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        ScanPayVault.PermitSig memory ps = _ps(it);
        // garbage / empty signatures: a signature check would give InvalidSignature, so InvalidQuote proves order
        it.inrPaise = 0;
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vault.offramp(it, "", "", ps);
        it.inrPaise = 830_000;
        it.rateE6 = 0;
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vault.offramp(it, hex"deadbeef", hex"01", ps);
        it.rateE6 = 83_000_000;
        it.qrCommitment = bytes32(0);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vault.offramp(it, new bytes(65), new bytes(65), ps);
    }

    function test_Quote_OverDifferentIntentReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        bytes memory staleQuote = _signQuote(vault, quotePk, it);
        // user now signs a different inrPaise; the quote does not cover it
        it.inrPaise = 900_000;
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _ps(it);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, staleQuote, ps);
        assertEq(_st(ID1), uint8(NONE));
    }

    function test_Quote_OverDifferentRateIdAmountDeadlineUserReverts() public {
        ScanPayVault.OfframpIntent memory base = _intent(ID1, user, 5 * UNIT, QR);
        bytes memory staleQuote = _signQuote(vault, quotePk, base);
        for (uint256 i; i < 4; i++) {
            ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
            if (i == 0) it.rateE6 += 1;
            if (i == 1) it.id = ID2;
            if (i == 2) it.amount = 6 * UNIT;
            if (i == 3) it.deadline += 1;
            bytes memory sig = _signIntent(vault, userPk, it);
            ScanPayVault.PermitSig memory ps = _ps(it);
            vm.expectRevert(ScanPayVault.InvalidQuote.selector);
            vm.prank(relayer);
            vault.offramp(it, sig, staleQuote, ps);
        }
    }

    function test_Quote_MalformedLengthReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        bytes memory good = _signQuote(vault, quotePk, it);
        ScanPayVault.PermitSig memory ps = _ps(it);
        uint256[5] memory lens = [uint256(1), 32, 64, 66, 97];
        for (uint256 i; i < lens.length; i++) {
            bytes memory bad = new bytes(lens[i]);
            for (uint256 k; k < lens[i]; k++) bad[k] = k < 65 ? good[k] : bytes1(0);
            vm.expectRevert(ScanPayVault.InvalidQuote.selector);
            vm.prank(relayer);
            vault.offramp(it, sig, bad, ps);
        }
        // truncated by one byte
        bytes memory trunc = new bytes(64);
        for (uint256 k; k < 64; k++) trunc[k] = good[k];
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, trunc, ps);
    }

    function test_Quote_MalleatedHighSQuoteReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        bytes32 d = vault.hashIntent(it);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(quotePk, d);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _ps(it);
        bytes memory flipped = abi.encodePacked(r, bytes32(SECP_N - uint256(s)), v == 27 ? uint8(28) : uint8(27));
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, flipped, ps);
        // the genuine signature still works
        vm.prank(relayer);
        vault.offramp(it, sig, abi.encodePacked(r, s, v), ps);
        assertEq(_st(ID1), uint8(LOCKED));
    }

    // ------------------------------------------------------------ qrCommitment only
    function test_Commitment_EventHasOnlyCommitmentAndFourDataWords() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 12 * UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.recordLogs();
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
        Vm.Log[] memory logs = vm.getRecordedLogs();

        uint256 found;
        for (uint256 i; i < logs.length; i++) {
            if (logs[i].emitter != address(vault)) continue;
            if (logs[i].topics[0] != ScanPayVault.OfframpLocked.selector) continue;
            found++;
            assertEq(logs[i].topics.length, 3, "sig + id + user");
            assertEq(logs[i].topics[1], ID1);
            assertEq(logs[i].topics[2], bytes32(uint256(uint160(user))));
            assertEq(logs[i].data.length, 4 * 32, "amount, inrPaise, rateE6, qrCommitment only");
            (uint256 amt, uint64 inr, uint64 rate, bytes32 qc) =
                abi.decode(logs[i].data, (uint256, uint64, uint64, bytes32));
            assertEq(amt, 12 * UNIT);
            assertEq(inr, it.inrPaise);
            assertEq(rate, it.rateE6);
            assertEq(qc, keccak256(abi.encode(SALT, QR)));
            assertFalse(_contains(logs[i].data, bytes(QR)), "no plaintext QR in event");
            assertFalse(_contains(logs[i].data, abi.encodePacked(SALT)), "no salt in event");
        }
        assertEq(found, 1);
    }

    function test_Commitment_StoredInOfframps() public {
        ScanPayVault.OfframpIntent memory it = _lock(ID1, AMT);
        assertEq(_off(ID1).qrCommitment, it.qrCommitment);
        assertEq(_off(ID1).qrCommitment, _commit(SALT, QR));
        (,,,,,, bytes32 qc) = vault.offramps(ID1);
        assertEq(qc, _commit(SALT, QR));
    }

    function test_Commitment_SameQrDifferentSaltGivesDifferentCommitment() public view {
        assertTrue(_commit(SALT, QR) != _commit(keccak256("other-salt"), QR));
        assertTrue(_commit(SALT, QR) != _commit(SALT, "upi://pay?pa=other@upi"));
    }

    function test_Commitment_RawQrAndSaltNeverInCalldata() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        bytes memory cd = abi.encodeCall(vault.offramp, (it, sig, qsig, ps));
        assertFalse(_contains(cd, bytes(QR)), "raw QR text in calldata");
        assertFalse(_contains(cd, abi.encodePacked(SALT)), "salt in calldata");
        assertFalse(_contains(cd, bytes("upi://")), "payee scheme in calldata");
        // positive controls for the search helper
        assertTrue(_contains(cd, abi.encodePacked(it.qrCommitment)), "commitment is in calldata");
        assertTrue(_contains(abi.encodePacked("xx", QR, "yy"), bytes(QR)));
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Commitment_TamperedAfterSigningFailsUserSignature() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        it.qrCommitment = _commit(SALT, "upi://pay?pa=attacker@upi");
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Commitment_StaleQuoteWithFreshUserSigFailsQuote() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        bytes memory staleQuote = _signQuote(vault, quotePk, it);
        it.qrCommitment = _commit(SALT, "upi://pay?pa=other@upi");
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _ps(it);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, staleQuote, ps);
    }

    // ------------------------------------------------------------ id squatting
    function test_Squat_AttackerWithoutQuoteCannotTakeVictimId() public {
        (address attacker, uint256 attackerPk) = makeAddrAndKey("attacker");
        _mint(attacker, 10 * UNIT);
        ScanPayVault.OfframpIntent memory evil = _intent(ID1, attacker, 1, "upi://pay?pa=attacker@upi");
        bytes memory aSig = _signIntent(vault, attackerPk, evil);
        ScanPayVault.PermitSig memory aPs =
            _signPermit(tokAddr, attackerPk, address(vault), evil.amount, block.timestamp + 1 hours);

        // (a) empty quote
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(attacker);
        vault.offramp(evil, aSig, "", aPs);
        // (b) quote signed by the attacker's own key
        bytes memory aQuote = _signQuote(vault, attackerPk, evil);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(attacker);
        vault.offramp(evil, aSig, aQuote, aPs);
        assertEq(_st(ID1), uint8(NONE), "id must remain unclaimed");

        // (c) attacker cannot forge the victim's signature either
        ScanPayVault.OfframpIntent memory forged = _intent(ID1, user, 1, QR);
        bytes memory forgedSig = _signIntent(vault, attackerPk, forged);
        bytes memory forgedQuote = _signQuote(vault, quotePk, forged);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vm.prank(attacker);
        vault.offramp(forged, forgedSig, forgedQuote, aPs);

        // victim's legitimate offramp with the same id succeeds
        _lock(ID1, 7 * UNIT);
        assertEq(_st(ID1), uint8(LOCKED));
        assertEq(_off(ID1).user, user);
        assertEq(_off(ID1).amount, 7 * UNIT);
    }

    // ------------------------------------------------------------ gasless relay
    function test_Gasless_UserWithZeroNativeBalanceSendsNoTransaction() public {
        vm.deal(user, 0);
        vm.deal(relayer, 1 ether);
        assertEq(address(user).balance, 0);
        uint64 nonceBefore = vm.getNonce(user);
        uint256 userTok = _bal(user);
        uint256 vaultTok = _bal(address(vault));
        uint256 amt = 25 * UNIT;

        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amt, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        assertTrue(relayer != user);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);

        assertEq(_bal(user), userTok - amt);
        assertEq(_bal(address(vault)), vaultTok + amt);
        assertEq(vm.getNonce(user), nonceBefore, "user sent no transaction");
        assertEq(address(user).balance, 0, "user still has zero native balance");
        assertEq(address(vault).balance, 0);
        assertEq(_st(ID1), uint8(LOCKED));
        assertEq(_off(ID1).user, user);
    }

    // ------------------------------------------------------------ helpers
    function _contains(bytes memory hay, bytes memory needle) internal pure returns (bool) {
        if (needle.length == 0 || hay.length < needle.length) return false;
        for (uint256 i; i + needle.length <= hay.length; i++) {
            bool ok = true;
            for (uint256 j; j < needle.length; j++) {
                if (hay[i + j] != needle[j]) {
                    ok = false;
                    break;
                }
            }
            if (ok) return true;
        }
        return false;
    }
}
