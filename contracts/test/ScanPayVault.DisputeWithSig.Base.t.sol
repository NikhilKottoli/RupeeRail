// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultDisputeTimeoutBase} from "./ScanPayVaultDisputeTimeoutBase.t.sol";

/// @dev Shared helpers for the relayed (EIP-712 signed) dispute suites. No tests here.
/// Do NOT also inherit ScanPayVaultFuzzBase: both define `_complete(bytes32)`.
abstract contract ScanPayVaultDisputeSigBase is ScanPayVaultDisputeTimeoutBase {
    bytes32 internal constant ID4 = keccak256("id4");
    bytes32 internal constant ID5 = keccak256("id5");
    bytes32 internal constant ID6 = keccak256("id6");
    uint256 internal constant SECP_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141;

    /// @dev User-style signature (r, s, v) over vault.hashDispute(id, usr, deadline) by `pk`.
    function _signDispute(uint256 pk, bytes32 id, address usr, uint64 deadline) internal view returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, vault.hashDispute(id, usr, deadline));
        return abi.encodePacked(r, s, v);
    }

    /// @dev The honest user's signature over (id, user, deadline).
    function _userSig(bytes32 id, uint64 deadline) internal view returns (bytes memory) {
        return _signDispute(userPk, id, user, deadline);
    }

    function _deadlineIn(uint256 secs) internal view returns (uint64) {
        return uint64(block.timestamp + secs);
    }

    /// @dev A separate relayer submits; the user never transacts.
    function _relayDispute(bytes32 id, uint64 deadline, bytes memory sig) internal {
        vm.prank(relayer);
        vault.disputeOfframpWithSig(id, deadline, sig);
    }

    /// @dev Honest user signs with a deadline 1h out; relayer submits.
    function _relayedDispute(bytes32 id) internal {
        uint64 d = _deadlineIn(1 hours);
        _relayDispute(id, d, _userSig(id, d));
    }

    function _lockCompleteRelayedDispute(bytes32 id) internal returns (uint256 dl) {
        _lockComplete(id, AMT);
        _relayedDispute(id);
        dl = _dl(id).releasableAt;
    }

    function _expectWindowExpired(uint256 rel) internal {
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeWindowExpired.selector, rel));
    }

    function _expectIntentExpired(uint64 d) internal {
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.IntentExpired.selector, d));
    }

    function _expectBadSig() internal {
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
    }

    /// @dev Hash of every user-visible piece of state touched by a dispute, for path-parity comparisons.
    function _stateHash(bytes32 id) internal view returns (bytes32) {
        return keccak256(abi.encode(_offHash(id), _dlHash(id), _totalsHash()));
    }

    function _offHash(bytes32 id) private view returns (bytes32) {
        Off memory o = _off(id);
        return keccak256(abi.encode(o));
    }

    function _dlHash(bytes32 id) private view returns (bytes32) {
        Dl memory d = _dl(id);
        return keccak256(abi.encode(d.completableAt, d.reclaimableAt, d.disputeWindow, d.releasableAt));
    }

    function _totalsHash() private view returns (bytes32) {
        return keccak256(
            abi.encode(
                vault.escrowedTotal(), vault.heldTotal(), vault.poolBalance(),
                _bal(address(vault)), _bal(user), _bal(relayer)
            )
        );
    }
}
