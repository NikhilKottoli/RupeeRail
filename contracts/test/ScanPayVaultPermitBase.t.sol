// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultBase} from "./ScanPayVaultBase.t.sol";

/// @dev Shared helpers for the token-variant (permit / wallet / gasless) test files. No tests here.
abstract contract ScanPayVaultPermitBase is ScanPayVaultBase {
    uint256 internal constant AMT = 50 * UNIT;
    bytes32 internal constant ID4 = keccak256("id4");
    bytes32 internal constant ID5 = keccak256("id5");
    bytes32 internal constant ID6 = keccak256("id6");

    /// @dev Re-points the fixture at a different token (fresh vault) and funds user + funder.
    function _use(address t) internal {
        _deployVault(t);
        _mint(user, 10_000 * UNIT);
        _mint(funder, 10_000 * UNIT);
    }

    /// @dev User signature and quote signature (both over hashIntent) for `it`.
    function _sigs(ScanPayVault.OfframpIntent memory it) internal view returns (bytes memory, bytes memory) {
        return (_signIntent(vault, userPk, it), _signQuote(vault, quotePk, it));
    }

    function _tokTransfer(address from, address to, uint256 amt) internal {
        vm.prank(from);
        (bool ok,) = tokAddr.call(abi.encodeWithSignature("transfer(address,uint256)", to, amt));
        require(ok, "transfer failed");
    }

    function _completeWith(bytes32 id) internal {
        vm.prank(settler);
        vault.completeOfframp(id, _refOf(id));
    }

    /// @dev Every vault path with the current token:
    /// complete(held)->reverse, refund, complete->release, dispute->refund, dispute->pool,
    /// fundPool/onramp/withdrawPool, sweepSurplus, reclaim by anyone.
    function _fullFlow() internal {
        // 1: complete moves escrow -> held (not pool); reverse returns it to the user
        _lock(ID1, AMT);
        skip(15);
        _completeWith(ID1);
        assertEq(vault.escrowedTotal(), 0);
        assertEq(vault.heldTotal(), AMT);
        assertEq(vault.poolBalance(), 0);
        uint256 b = _bal(user);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(_bal(user), b + AMT);
        assertEq(vault.heldTotal(), 0);

        // 2: refund
        _lock(ID2, AMT);
        b = _bal(user);
        vm.prank(settler);
        vault.refundOfframp(ID2, 2);
        assertEq(_bal(user), b + AMT);

        // 3: complete -> held, then release (anyone) -> pool
        _lockComplete(ID3, AMT);
        vm.warp(_dl(ID3).releasableAt);
        vm.prank(rando);
        vault.releaseOfframp(ID3);
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), AMT);

        // 4: dispute -> arbiter refunds the user
        _lockComplete(ID4, AMT);
        vm.prank(user);
        vault.disputeOfframp(ID4);
        b = _bal(user);
        vm.prank(arbiter);
        vault.resolveDispute(ID4, true);
        assertEq(_bal(user), b + AMT);

        // 5: dispute -> arbiter releases to the pool
        _lockComplete(ID5, AMT);
        vm.prank(user);
        vault.disputeOfframp(ID5);
        vm.prank(arbiter);
        vault.resolveDispute(ID5, false);
        assertEq(vault.poolBalance(), 2 * AMT);
        assertEq(vault.heldTotal(), 0);

        // treasury: fundPool -> onramp -> withdrawPool
        _fund(200 * UNIT);
        vm.prank(settler);
        vault.onramp(keccak256("ord"), wallet, 80 * UNIT, 1);
        assertEq(_bal(wallet), 80 * UNIT);
        vm.prank(owner);
        vault.withdrawPool(owner, 120 * UNIT);
        assertEq(_bal(owner), 120 * UNIT);
        assertEq(vault.poolBalance(), 100 * UNIT);

        // surplus: direct transfer is swept, accounting untouched
        address sink = makeAddr("sink");
        _tokTransfer(funder, address(vault), 7 * UNIT);
        vm.prank(owner);
        vault.sweepSurplus(sink);
        assertEq(_bal(sink), 7 * UNIT);
        assertEq(vault.poolBalance(), 100 * UNIT);

        // 6: reclaim by anyone after reclaimableAt
        _lock(ID6, AMT);
        skip(24 hours);
        b = _bal(user);
        vm.prank(rando);
        vault.reclaimOfframp(ID6);
        assertEq(_bal(user), b + AMT);

        // 7: dispute -> arbiter silent -> anyone claims the timeout; only the user is paid
        bytes32 id7 = keccak256("id7");
        _lockComplete(id7, AMT);
        vm.prank(user);
        vault.disputeOfframp(id7);
        vm.warp(_dl(id7).releasableAt);
        b = _bal(user);
        vm.prank(rando);
        vault.claimDisputeTimeout(id7);
        assertEq(_bal(user), b + AMT);
        assertEq(_bal(rando), 0);
        assertEq(vault.heldTotal(), 0);

        assertEq(_bal(address(vault)), 100 * UNIT);
        assertEq(_bal(address(vault)), vault.escrowedTotal() + vault.heldTotal() + vault.poolBalance());
        _assertAccounting();
    }
}
