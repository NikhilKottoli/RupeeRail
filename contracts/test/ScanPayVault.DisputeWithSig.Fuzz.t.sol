// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultDisputeSigBase} from "./ScanPayVault.DisputeWithSig.Base.t.sol";

/// @dev Relayed dispute fuzzing: signature deadline, dispute-window boundary, timeout snapshot, signer, caller.
contract ScanPayVaultDisputeWithSigFuzzTest is ScanPayVaultDisputeSigBase {
    /// @dev Submits and asserts accepted-iff-`ok`; otherwise expects `errData` and an unchanged COMPLETED state.
    function _submitExpecting(bytes32 id, uint64 d, bool ok, bytes memory errData) internal {
        bytes memory sig = _userSig(id, d);
        if (ok) {
            _relayDispute(id, d, sig);
            assertEq(_st(id), uint8(DISPUTED));
        } else {
            vm.expectRevert(errData);
            _relayDispute(id, d, sig);
            assertEq(_st(id), uint8(COMPLETED));
        }
    }

    // ------------------------------------------------------------ signature deadline
    function testFuzz_SigDeadline_AcceptedIffNotInPast(uint256 off) public {
        _lockComplete(ID1, AMT);
        off = bound(off, 0, 4 hours);
        uint64 d = uint64(block.timestamp - 2 hours + off);
        bool ok = d >= block.timestamp;
        _submitExpecting(ID1, d, ok, abi.encodeWithSelector(ScanPayVault.IntentExpired.selector, d));
    }

    function testFuzz_SigDeadline_BoundaryAroundNow(uint8 x) public {
        _lockComplete(ID1, AMT);
        uint64 d = uint64(block.timestamp - 2 + (x % 5));
        bool ok = d >= block.timestamp;
        _submitExpecting(ID1, d, ok, abi.encodeWithSelector(ScanPayVault.IntentExpired.selector, d));
    }

    function testFuzz_SigDeadline_AnyFutureDeadlineIncludingMax(uint64 d) public {
        _lockComplete(ID1, AMT);
        d = uint64(bound(d, block.timestamp, type(uint64).max));
        _submitExpecting(ID1, d, true, "");
    }

    // ------------------------------------------------------------ dispute window
    function testFuzz_Window_AcceptedIffBeforeReleasableAt(uint256 off) public {
        _lockComplete(ID1, AMT);
        uint256 rel = _dl(ID1).releasableAt;
        uint256 t = rel - 3 hours + bound(off, 0, 6 hours);
        vm.warp(t);
        _submitExpecting(
            ID1, _deadlineIn(1 hours), t < rel, abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, rel)
        );
    }

    function testFuzz_Window_BoundaryAroundReleasableAt(uint8 x) public {
        _lockComplete(ID1, AMT);
        uint256 rel = _dl(ID1).releasableAt;
        uint256 t = rel - 2 + (x % 5);
        vm.warp(t);
        _submitExpecting(
            ID1, _deadlineIn(1 hours), t < rel, abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, rel)
        );
    }

    function testFuzz_Window_FromConfiguredDisputeWindow(uint256 window, uint256 off) public {
        window = bound(window, 1 hours, 30 days);
        vm.prank(owner);
        vault.setDisputeWindow(window);
        _lockComplete(ID1, AMT);
        uint256 rel = _dl(ID1).releasableAt;
        assertEq(rel, _completedAt(ID1) + window);
        uint256 t = rel - 30 minutes + bound(off, 0, 1 hours);
        vm.warp(t);
        _submitExpecting(
            ID1, _deadlineIn(1 hours), t < rel, abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, rel)
        );
    }

    // ------------------------------------------------------------ timeout snapshot
    function testFuzz_TimeoutSnapshot_EqualsDisputeTimePlusTimeout(uint256 timeout, uint256 elapsed, uint256 timeout2)
        public
    {
        timeout = bound(timeout, 1 days, 30 days);
        timeout2 = bound(timeout2, 1 days, 30 days);
        _setTimeout(timeout);
        _lockComplete(ID1, AMT);
        uint256 rel = _dl(ID1).releasableAt;
        uint256 t = block.timestamp + bound(elapsed, 0, rel - 1 - block.timestamp);
        vm.warp(t);
        _relayedDispute(ID1);
        uint256 dl = _dl(ID1).releasableAt;
        assertEq(dl, t + timeout, "deadline = dispute time + timeout");

        _setTimeout(timeout2); // later config changes never move the snapshot
        assertEq(_dl(ID1).releasableAt, dl);

        vm.warp(dl - 1);
        _expectTooEarly(dl);
        _claim(rando, ID1);
        vm.warp(dl);
        uint256 before = _bal(user);
        _claim(rando, ID1);
        assertEq(_bal(user), before + AMT);
    }

    function testFuzz_TimeoutSnapshot_SameAsDirectDispute(uint256 timeout, uint256 elapsed) public {
        timeout = bound(timeout, 1 days, 30 days);
        _setTimeout(timeout);
        _lockComplete(ID1, AMT);
        _lockComplete(ID2, AMT);
        uint256 rel = _dl(ID1).releasableAt;
        vm.warp(block.timestamp + bound(elapsed, 0, rel - 1 - block.timestamp));
        _relayedDispute(ID1);
        _dispute(ID2);
        assertEq(_dl(ID1).releasableAt, _dl(ID2).releasableAt);
    }

    // ------------------------------------------------------------ signer / caller
    function testFuzz_AnyOtherKeyCannotSign(uint256 pk) public {
        pk = bound(pk, 1, SECP_N - 1);
        vm.assume(pk != userPk);
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _signDispute(pk, ID1, user, d);
        _expectBadSig();
        _relayDispute(ID1, d, sig);
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function testFuzz_AnyCallerMaySubmitTheUsersSignature(address caller) public {
        vm.assume(caller != address(vault) && caller != address(vm) && uint160(caller) > 0xffff);
        vm.assume(caller != 0x000000000000000000636F6e736F6c652e6c6f67);
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _userSig(ID1, d);
        uint256 callerBal = _bal(caller);
        vm.prank(caller);
        vault.disputeOfframpWithSig(ID1, d, sig);
        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(_bal(caller), callerBal, "submitter receives no funds");
    }

    function testFuzz_ArbitrarySignatureBytesNeverDispute(bytes calldata junk) public {
        _lockComplete(ID1, AMT);
        uint64 d = _deadlineIn(1 hours);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        _relayDispute(ID1, d, junk);
        assertEq(_st(ID1), uint8(COMPLETED));
    }
}
