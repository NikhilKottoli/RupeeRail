// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultUnitBase} from "./ScanPayVaultUnitBase.t.sol";

/// @dev Shared helpers for the dispute-timeout suites. No tests here.
/// Do NOT also inherit ScanPayVaultFuzzBase: both define `_complete(bytes32)`.
abstract contract ScanPayVaultDisputeTimeoutBase is ScanPayVaultUnitBase {
    /// @dev Lock + complete + dispute `id` (amount AMT) and return its resolve deadline.
    function _disputed(bytes32 id) internal returns (uint256 dl) {
        _lockCompleteDispute(id, AMT);
        dl = _dl(id).releasableAt;
    }

    function _expectExpired(uint256 dl) internal {
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeResolutionExpired.selector, dl));
    }

    function _expectTooEarly(uint256 dl) internal {
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.TooEarly.selector, dl));
    }

    function _setTimeout(uint256 value) internal {
        vm.prank(owner);
        vault.setDisputeTimeout(value);
    }

    function _claim(address caller, bytes32 id) internal {
        vm.prank(caller);
        vault.claimDisputeTimeout(id);
    }

    function _resolve(bytes32 id, bool refundUser) internal {
        vm.prank(arbiter);
        vault.resolveDispute(id, refundUser);
    }
}
