// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultBase} from "./ScanPayVaultBase.t.sol";

/// @dev Extra constants/helpers shared by the unit-test files only. No tests here.
abstract contract ScanPayVaultUnitBase is ScanPayVaultBase {
    uint256 internal constant AMT = 40 * UNIT;
    ScanPayVault.Status internal constant NONE = ScanPayVault.Status.NONE;
    ScanPayVault.Status internal constant LOCKED = ScanPayVault.Status.LOCKED;
    ScanPayVault.Status internal constant COMPLETED = ScanPayVault.Status.COMPLETED;
    ScanPayVault.Status internal constant REFUNDED = ScanPayVault.Status.REFUNDED;
    ScanPayVault.Status internal constant REVERSED = ScanPayVault.Status.REVERSED;
    ScanPayVault.Status internal constant RELEASED = ScanPayVault.Status.RELEASED;
    ScanPayVault.Status internal constant DISPUTED = ScanPayVault.Status.DISPUTED;

    function _complete(bytes32 id) internal {
        vm.prank(settler);
        vault.completeOfframp(id, _refOf(id));
    }

    function _dispute(bytes32 id) internal {
        vm.prank(user);
        vault.disputeOfframp(id);
    }

    function _lockCompleteDispute(bytes32 id, uint256 amount) internal {
        _lockComplete(id, amount);
        _dispute(id);
    }

    function _pause() internal {
        vm.prank(owner);
        vault.setPaused(true);
    }

    function _st(bytes32 id) internal view returns (uint8) {
        return uint8(_status(id));
    }

    /// @dev Intent+signatures that are valid for `it` (user signs intent, quoteSigner quotes, user permit).
    function _offrampCall(ScanPayVault.OfframpIntent memory it) internal {
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
    }
}
