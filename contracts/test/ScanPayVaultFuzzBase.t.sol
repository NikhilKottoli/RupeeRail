// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultBase} from "./ScanPayVaultBase.t.sol";

/// @dev Helpers shared by the fuzz test files. No tests here.
abstract contract ScanPayVaultFuzzBase is ScanPayVaultBase {
    uint256 internal constant U96 = type(uint96).max;

    /// @dev Signs `it` (user, quote, matching permit) and relays it, optionally expecting a revert.
    function _tryOfframp(ScanPayVault.OfframpIntent memory it, bytes4 expectedErr, bytes memory errArgs) internal {
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        if (expectedErr != bytes4(0)) vm.expectRevert(bytes.concat(expectedErr, errArgs));
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
    }

    function _complete(bytes32 id) internal {
        vm.prank(settler);
        vault.completeOfframp(id, _refOf(id));
    }
}
