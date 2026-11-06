// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultDisputeSigBase} from "./ScanPayVault.DisputeWithSig.Base.t.sol";

/// @dev Relayed dispute: every rejection path, check ordering, replay and signature-binding rules.
contract ScanPayVaultDisputeWithSigRevertTest is ScanPayVaultDisputeSigBase {
    uint256 internal constant STRANGER_PK = uint256(keccak256("some-random-signer"));

    function _lockedCompleted() internal returns (uint64 d, bytes memory sig) {
        _lockComplete(ID1, AMT);
        d = _deadlineIn(1 hours);
        sig = _userSig(ID1, d);
    }

    function _assertStillCompleted() internal view {
        assertEq(_st(ID1), uint8(COMPLETED));
        assertEq(vault.heldTotal(), AMT);
    }

    // ------------------------------------------------------------ signature binding
    function test_Revert_WrongSignerRandomKey() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _signDispute(STRANGER_PK, ID1, user, d);
        _expectBadSig();
        _relayDispute(ID1, d, sig);
        _assertStillCompleted();
    }

    function test_Revert_SignatureForDifferentId() public {
        _lockComplete(ID1, AMT);
        _lockComplete(ID2, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sigForId2 = _userSig(ID2, d);
        _expectBadSig();
        _relayDispute(ID1, d, sigForId2);
        assertEq(_st(ID1), uint8(COMPLETED));
        assertEq(vault.heldTotal(), 2 * AMT);
        // and it works on the id it was made for
        _relayDispute(ID2, d, sigForId2);
        assertEq(_st(ID2), uint8(DISPUTED));
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_Revert_DigestNamesADifferentUserAddress() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        // user's key signs a digest that names `rando` as the user
        bytes memory sig = _signDispute(userPk, ID1, rando, d);
        _expectBadSig();
        _relayDispute(ID1, d, sig);
        // and a different address's own key signing its own digest cannot dispute someone else's offramp
        (address other, uint256 otherPk) = makeAddrAndKey("other-user");
        bytes memory sig2 = _signDispute(otherPk, ID1, other, d);
        _expectBadSig();
        _relayDispute(ID1, d, sig2);
        _assertStillCompleted();
    }

    function test_Revert_DifferentDeadlineThanSigned() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _userSig(ID1, d);
        _expectBadSig();
        _relayDispute(ID1, d + 1, sig);
        _expectBadSig();
        _relayDispute(ID1, d - 1, sig);
        _expectBadSig();
        _relayDispute(ID1, type(uint64).max, sig);
        _assertStillCompleted();
        _relayDispute(ID1, d, sig);
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    /// @dev Index: 0 quote signer, 1 settler, 2 arbiter, 3 owner, 4 guardian; each one a known-key address.
    function _rotateRolesToKeys() internal returns (uint256[5] memory pks, address[5] memory signers) {
        pks[0] = quotePk;
        signers[0] = quoteSigner;
        (signers[1], pks[1]) = makeAddrAndKey("settler-key");
        (signers[2], pks[2]) = makeAddrAndKey("arbiter-key");
        (signers[3], pks[3]) = makeAddrAndKey("owner-key");
        (signers[4], pks[4]) = makeAddrAndKey("guardian-key");
        vm.startPrank(owner);
        vault.setSettler(signers[1]);
        vault.setArbiter(signers[2]);
        vault.setGuardian(signers[4]);
        vault.transferOwnership(signers[3]);
        vm.stopPrank();
        vm.prank(signers[3]);
        vault.acceptOwnership();
    }

    function test_Revert_ProtocolRoleKeysDoNotAuthorize() public {
        _lockComplete(ID1, AMT);
        (uint256[5] memory pks, address[5] memory signers) = _rotateRolesToKeys();
        uint64 d = _deadlineIn(1 hours);
        for (uint256 i; i < pks.length; i++) {
            bytes memory sig = _signDispute(pks[i], ID1, user, d);
            _expectBadSig();
            _relayDispute(ID1, d, sig);
            // even when the role itself submits its own signature
            _expectBadSig();
            vm.prank(signers[i]);
            vault.disputeOfframpWithSig(ID1, d, sig);
        }
        _assertStillCompleted();
    }

    function test_Revert_OfframpIntentSignatureCannotBeReusedAsDispute() public {
        ScanPayVault.OfframpIntent memory it = _lock(ID1, AMT);
        skip(15);
        _complete(ID1);
        bytes memory intentSig = _signIntent(vault, userPk, it); // user's genuine offramp-intent signature
        _expectBadSig();
        _relayDispute(ID1, it.deadline, intentSig);
        // also the quote signature (same digest family) is no use
        bytes memory quoteSig = _signQuote(vault, quotePk, it);
        _expectBadSig();
        _relayDispute(ID1, it.deadline, quoteSig);
        _assertStillCompleted();
    }

    function test_Revert_RawStructHashWithoutDomainIsRejected() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes32 sh = keccak256(abi.encode(vault.DISPUTE_INTENT_TYPEHASH(), ID1, user, d));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(userPk, sh); // no EIP-712 domain wrapper
        _expectBadSig();
        _relayDispute(ID1, d, abi.encodePacked(r, s, v));
        _assertStillCompleted();
    }

    function test_Revert_SignatureMadeForAnotherVaultInstance() public {
        ScanPayVault vault2 = new ScanPayVault(IERC20(tokAddr), owner, settler, quoteSigner, arbiter);
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        assertTrue(vault2.hashDispute(ID1, user, d) != vault.hashDispute(ID1, user, d), "domains differ");
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(userPk, vault2.hashDispute(ID1, user, d));
        _expectBadSig();
        _relayDispute(ID1, d, abi.encodePacked(r, s, v));
        _assertStillCompleted();
    }

    // ------------------------------------------------------------ malformed signatures
    function test_Revert_MalformedEmptyAndShortSignatures() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes memory good = _userSig(ID1, d);
        bytes[] memory bad = new bytes[](7);
        bad[0] = "";
        bad[1] = hex"00";
        bad[2] = new bytes(31);
        bad[3] = new bytes(64); // zero compact signature
        bad[4] = new bytes(65); // zero r/s/v
        bad[5] = abi.encodePacked(good, hex"00"); // 66 bytes
        bad[6] = _truncate(good, 63); // one byte short
        for (uint256 i; i < bad.length; i++) {
            _expectBadSig();
            _relayDispute(ID1, d, bad[i]);
        }
        _assertStillCompleted();
    }

    function _truncate(bytes memory b, uint256 n) internal pure returns (bytes memory out) {
        out = new bytes(n);
        for (uint256 i; i < n; i++) out[i] = b[i];
    }

    function test_Revert_HighSMalleatedSignature() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(userPk, vault.hashDispute(ID1, user, d));
        assertLe(uint256(s), SECP_N / 2, "vm.sign yields low-s");
        bytes32 highS = bytes32(SECP_N - uint256(s));
        uint8 flipped = v == 27 ? 28 : 27;
        _expectBadSig();
        _relayDispute(ID1, d, abi.encodePacked(r, highS, flipped));
        _assertStillCompleted();
        // the canonical signature still works
        _relayDispute(ID1, d, abi.encodePacked(r, s, v));
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    // ------------------------------------------------------------ deadline
    function test_Revert_ExpiredDeadline() public {
        _lockComplete(ID1, AMT);
        uint64 d = uint64(block.timestamp - 1);
        bytes memory sig = _userSig(ID1, d);
        _expectIntentExpired(d);
        _relayDispute(ID1, d, sig);
        d = 0;
        sig = _userSig(ID1, d);
        _expectIntentExpired(d);
        _relayDispute(ID1, d, sig);
        _assertStillCompleted();
    }

    function test_Deadline_EqualToNowIsOkAndNowMinusOneIsNot() public {
        _lockComplete(ID1, AMT);
        uint64 now_ = uint64(block.timestamp);
        bytes memory sigPast = _userSig(ID1, now_ - 1);
        bytes memory sigNow = _userSig(ID1, now_);
        _expectIntentExpired(now_ - 1);
        _relayDispute(ID1, now_ - 1, sigPast);
        _relayDispute(ID1, now_, sigNow);
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    function test_Revert_SignedDeadlinePassesWhileWaiting() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(10 minutes);
        bytes memory sig = _userSig(ID1, d);
        skip(10 minutes + 1);
        _expectIntentExpired(d);
        _relayDispute(ID1, d, sig);
        _assertStillCompleted();
    }

    // ------------------------------------------------------------ status
    function _expectStatusRevert(bytes32 id, ScanPayVault.Status actual) internal {
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _userSig(id, d);
        _expectInvalidStatus(id, actual, COMPLETED);
        _relayDispute(id, d, sig);
    }

    function test_Revert_NotCompletedStatuses() public {
        // NONE: ID1 never locked
        _expectStatusRevert(ID1, NONE);
        // LOCKED
        _lock(ID2, AMT);
        // REFUNDED
        _lock(ID3, AMT);
        vm.prank(settler);
        vault.refundOfframp(ID3, 1);
        // REVERSED
        _lockComplete(ID4, AMT);
        vm.prank(settler);
        vault.reverseOfframp(ID4);
        // DISPUTED
        _lockCompleteDispute(ID6, AMT);
        // RELEASED
        _lockComplete(ID5, AMT);
        vm.warp(_dl(ID5).releasableAt);
        vault.releaseOfframp(ID5);

        _expectStatusRevert(ID2, LOCKED);
        _expectStatusRevert(ID3, REFUNDED);
        _expectStatusRevert(ID4, REVERSED);
        _expectStatusRevert(ID5, RELEASED);
        _expectStatusRevert(ID6, DISPUTED);
    }

    function test_Order_StatusCheckedBeforeDeadline() public {
        uint64 d = uint64(block.timestamp - 1); // expired, and the id does not exist
        bytes memory sig = _userSig(ID1, d);
        _expectInvalidStatus(ID1, NONE, COMPLETED);
        _relayDispute(ID1, d, sig);
    }

    // ------------------------------------------------------------ dispute window
    function test_Window_RevertsAtReleasableAtAndAfter() public {
        _lockComplete(ID1, AMT);
        uint64 rel = _dl(ID1).releasableAt;
        uint256[5] memory offsets = [uint256(0), 1, 1 hours, 3 days, 365 days];
        for (uint256 i; i < offsets.length; i++) {
            vm.warp(uint256(rel) + offsets[i]);
            uint64 d = _deadlineIn(1 hours);
            bytes memory sig = _userSig(ID1, d);
            _expectWindowExpired(rel);
            _relayDispute(ID1, d, sig);
        }
        assertEq(_st(ID1), uint8(COMPLETED));
        // the window is genuinely closed: the funds are now releasable into the pool
        vault.releaseOfframp(ID1);
        assertEq(_st(ID1), uint8(RELEASED));
    }

    function test_Window_OkAtReleasableAtMinusOne() public {
        _lockComplete(ID1, AMT);
        uint64 rel = _dl(ID1).releasableAt;
        vm.warp(rel - 1);
        _relayedDispute(ID1);
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    // ------------------------------------------------------------ check order
    function test_Order_ExpiredDeadlineBeforeWindowBeforeSignature() public {
        _lockComplete(ID1, AMT);
        uint64 rel = _dl(ID1).releasableAt;
        vm.warp(rel); // window closed
        uint64 expired = uint64(block.timestamp - 1);
        // expired deadline AND closed window AND bad sig -> IntentExpired
        bytes memory badExpired = _signDispute(STRANGER_PK, ID1, user, expired);
        _expectIntentExpired(expired);
        _relayDispute(ID1, expired, badExpired);
        // valid deadline, closed window, bad sig -> DisputeWindowExpired (before signature check)
        uint64 ok = _deadlineIn(1 hours);
        bytes memory badOk = _signDispute(STRANGER_PK, ID1, user, ok);
        _expectWindowExpired(rel);
        _relayDispute(ID1, ok, badOk);
        _expectWindowExpired(rel);
        _relayDispute(ID1, ok, "");
    }

    function test_Order_BadSignatureOnlyWhenEverythingElseIsFine() public {
        _lockComplete(ID1, AMT);
        uint64 ok = _deadlineIn(1 hours);
        bytes memory badOk = _signDispute(STRANGER_PK, ID1, user, ok);
        _expectBadSig();
        _relayDispute(ID1, ok, badOk);
    }

    // ------------------------------------------------------------ replay
    function test_Replay_SecondDisputeWithSameSignatureIsInvalidStatus() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _userSig(ID1, d);
        _relayDispute(ID1, d, sig);
        uint256 snapshotDeadline = _dl(ID1).releasableAt;
        skip(1 hours);
        _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
        _relayDispute(ID1, d, sig);
        // a freshly signed dispute is equally rejected, and the snapshot is never refreshed
        bytes memory fresh = _userSig(ID1, d + 1 hours);
        _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
        _relayDispute(ID1, d + 1 hours, fresh);
        _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
        _dispute(ID1);
        assertEq(_dl(ID1).releasableAt, snapshotDeadline, "resolve deadline never extended by replay");
    }

    function test_Replay_AfterResolutionStillInvalidStatus() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _userSig(ID1, d);
        _relayDispute(ID1, d, sig);
        _resolve(ID1, true);
        _expectInvalidStatus(ID1, REVERSED, COMPLETED);
        _relayDispute(ID1, d, sig);
    }

    function test_StaleSignatureCannotDisputeLaterOfframpWithAnotherId() public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _userSig(ID1, d);
        _lockComplete(ID2, AMT);
        _expectBadSig();
        _relayDispute(ID2, d, sig);
        assertEq(_st(ID2), uint8(COMPLETED));
    }

    function test_Revert_FailedDisputeLeavesStateUntouched() public {
        (uint64 d, bytes memory sig) = _lockedCompleted();
        uint256 relBefore = _dl(ID1).releasableAt;
        _expectBadSig();
        _relayDispute(ID1, d + 5, sig);
        assertEq(_dl(ID1).releasableAt, relBefore, "failed dispute leaves deadlines untouched");
        assertEq(vault.heldTotal(), AMT);
    }
}
