// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultDisputeTimeoutBase} from "./ScanPayVaultDisputeTimeoutBase.t.sol";

/// @dev Dispute timeout core behaviour: arbiter window, permissionless claim, pause, exclusivity.
contract ScanPayVaultDisputeTimeoutTest is ScanPayVaultDisputeTimeoutBase {
    // ------------------------------------------------------------ (a) arbiter window
    function test_Arbiter_RefundWorksOneSecondBeforeDeadline() public {
        uint256 dl = _disputed(ID1);
        vm.warp(dl - 1);
        uint256 userBefore = _bal(user);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.DisputeResolved(ID1, true, AMT);
        _resolve(ID1, true);
        assertEq(_bal(user), userBefore + AMT);
        assertEq(_st(ID1), uint8(REVERSED));
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 0);
        _assertAccounting();
    }

    function test_Arbiter_ReleaseWorksOneSecondBeforeDeadline() public {
        uint256 dl = _disputed(ID1);
        vm.warp(dl - 1);
        uint256 userBefore = _bal(user);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.DisputeResolved(ID1, false, AMT);
        _resolve(ID1, false);
        assertEq(_bal(user), userBefore);
        assertEq(_st(ID1), uint8(RELEASED));
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), AMT);
        _assertAccounting();
    }

    function test_Arbiter_BothWaysRevertExactlyAtDeadline() public {
        uint256 dl = _disputed(ID1);
        vm.warp(dl);
        _expectExpired(dl);
        _resolve(ID1, true);
        _expectExpired(dl);
        _resolve(ID1, false);
        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(vault.heldTotal(), AMT);
        assertEq(vault.poolBalance(), 0);
    }

    function test_Arbiter_BothWaysRevertAfterDeadline() public {
        uint256 dl = _disputed(ID1);
        uint256[4] memory offsets = [uint256(1), 1 hours, 30 days, 5 * 365 days];
        for (uint256 i; i < offsets.length; i++) {
            vm.warp(dl + offsets[i]);
            _expectExpired(dl);
            _resolve(ID1, true);
            _expectExpired(dl);
            _resolve(ID1, false);
        }
        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(vault.heldTotal(), AMT);
    }

    // ------------------------------------------------------------ (b) claim: too early / wrong status
    function test_Claim_RevertsTooEarlyUpToDeadlineMinusOne() public {
        uint256 dl = _disputed(ID1);
        uint256 disputedAt = block.timestamp;
        uint256[4] memory ts = [disputedAt, disputedAt + 1, disputedAt + 24 hours, dl - 1];
        for (uint256 i; i < ts.length; i++) {
            vm.warp(ts[i]);
            _expectTooEarly(dl);
            _claim(rando, ID1);
        }
        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(vault.heldTotal(), AMT);
        assertEq(_bal(address(vault)), AMT);
    }

    function test_Claim_RevertsInvalidStatusOnEveryNonDisputedStatus() public {
        // NONE
        _expectInvalidStatus(ID1, NONE, DISPUTED);
        _claim(rando, ID1);
        // LOCKED
        _lock(ID1, AMT);
        _expectInvalidStatus(ID1, LOCKED, DISPUTED);
        _claim(rando, ID1);
        // REFUNDED
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        _expectInvalidStatus(ID1, REFUNDED, DISPUTED);
        _claim(rando, ID1);
        // COMPLETED
        _lockComplete(ID2, AMT);
        _expectInvalidStatus(ID2, COMPLETED, DISPUTED);
        _claim(rando, ID2);
        // REVERSED (settler reversal)
        _lockComplete(ID3, AMT);
        vm.prank(settler);
        vault.reverseOfframp(ID3);
        _expectInvalidStatus(ID3, REVERSED, DISPUTED);
        _claim(rando, ID3);
        // RELEASED (window release)
        bytes32 id4 = keccak256("id4");
        _lockCompleteRelease(id4, AMT);
        skip(60 days); // far past any would-be deadline: status check still wins
        _expectInvalidStatus(id4, RELEASED, DISPUTED);
        _claim(rando, id4);
        _expectInvalidStatus(ID2, COMPLETED, DISPUTED);
        _claim(rando, ID2);
    }

    // ------------------------------------------------------------ (c) third-party claim pays only the user
    function _assertClaimPaysOnlyUser(address caller, bytes32 id) internal {
        uint256 userBefore = _bal(user);
        uint256 callerBefore = _bal(caller);
        uint256 vaultBefore = _bal(address(vault));
        uint256 heldBefore = vault.heldTotal();
        uint256 poolBefore = vault.poolBalance();
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.DisputeTimedOut(id, user, AMT);
        _claim(caller, id);
        assertEq(_bal(caller), callerBefore, "caller received nothing");
        assertEq(_bal(user), userBefore + AMT, "user up by exactly amount");
        assertEq(_bal(address(vault)), vaultBefore - AMT, "vault down by amount");
        assertEq(vault.heldTotal(), heldBefore - AMT, "heldTotal down by amount");
        assertEq(vault.poolBalance(), poolBefore, "pool unchanged");
        assertEq(_st(id), uint8(REVERSED));
        _assertAccounting();
    }

    function test_Claim_AnyThirdPartyPaysItemUser() public {
        address[6] memory callers = [rando, relayer, settler, owner, arbiter, guardian];
        bytes32[6] memory idsArr;
        uint256 lastDl;
        for (uint256 i; i < callers.length; i++) {
            idsArr[i] = bytes32(uint256(300 + i));
            lastDl = _disputed(idsArr[i]);
        }
        _fund(9 * UNIT);
        vm.warp(lastDl + 3);
        for (uint256 i; i < callers.length; i++) {
            _assertClaimPaysOnlyUser(callers[i], idsArr[i]);
        }
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 9 * UNIT);
        assertEq(_bal(address(vault)), 9 * UNIT);
    }

    function test_Claim_UserCanClaimOwnToo() public {
        uint256 dl = _disputed(ID1);
        vm.warp(dl);
        uint256 before = _bal(user);
        _claim(user, ID1);
        assertEq(_bal(user), before + AMT);
        assertEq(_st(ID1), uint8(REVERSED));
    }

    function test_Claim_WorksExactlyAtDeadline() public {
        uint256 dl = _disputed(ID1);
        vm.warp(dl);
        _assertClaimPaysOnlyUser(rando, ID1);
    }

    function test_Claim_PaysItemUserNotLockRelayerOrOtherUsers() public {
        (address other, uint256 otherPk) = makeAddrAndKey("otherUser");
        _mint(other, AMT);
        _relay(_intent(ID2, other, AMT, QR), otherPk, otherPk);
        skip(15);
        _complete(ID2);
        vm.prank(other);
        vault.disputeOfframp(ID2);
        uint256 dl = _dl(ID2).releasableAt;
        vm.warp(dl);
        uint256 userBefore = _bal(user);
        uint256 relayerBefore = _bal(relayer);
        _claim(user, ID2);
        assertEq(_bal(other), AMT, "item.user (other) refunded");
        assertEq(_bal(user), userBefore, "caller unchanged");
        assertEq(_bal(relayer), relayerBefore, "locking relayer unchanged");
    }

    // ------------------------------------------------------------ (d) works while paused
    function test_Claim_WorksWhilePausedByOwner() public {
        uint256 dl = _disputed(ID1);
        _pause();
        vm.warp(dl);
        _assertClaimPaysOnlyUser(rando, ID1);
        assertTrue(vault.paused());
    }

    function test_Claim_WorksWhilePausedByGuardian() public {
        uint256 dl = _disputed(ID1);
        vm.prank(owner);
        vault.setGuardian(guardian);
        vm.prank(guardian);
        vault.guardianPause();
        assertTrue(vault.paused());
        vm.warp(dl + 1 days);
        _assertClaimPaysOnlyUser(relayer, ID1);
    }

    function test_Arbiter_ResolveStillWorksWhilePausedBeforeDeadline() public {
        uint256 dl = _disputed(ID1);
        _pause();
        vm.warp(dl - 1);
        _resolve(ID1, true);
        assertEq(_st(ID1), uint8(REVERSED));
    }

    // ------------------------------------------------------------ (e) mutual exclusion
    function test_Claim_SecondClaimRevertsInvalidStatus() public {
        uint256 dl = _disputed(ID1);
        vm.warp(dl);
        _claim(rando, ID1);
        _expectInvalidStatus(ID1, REVERSED, DISPUTED);
        _claim(rando, ID1);
        vm.warp(dl + 365 days);
        _expectInvalidStatus(ID1, REVERSED, DISPUTED);
        _claim(user, ID1);
        assertEq(vault.heldTotal(), 0);
        assertEq(_bal(address(vault)), 0);
    }

    function test_Resolve_AfterClaimRevertsInvalidStatus() public {
        uint256 dl = _disputed(ID1);
        vm.warp(dl);
        _claim(rando, ID1);
        _expectInvalidStatus(ID1, REVERSED, DISPUTED);
        _resolve(ID1, true);
        _expectInvalidStatus(ID1, REVERSED, DISPUTED);
        _resolve(ID1, false);
        assertEq(vault.poolBalance(), 0);
    }

    function test_Claim_AfterArbiterRefundRevertsInvalidStatus() public {
        uint256 dl = _disputed(ID1);
        vm.warp(dl - 1);
        _resolve(ID1, true);
        vm.warp(dl);
        _expectInvalidStatus(ID1, REVERSED, DISPUTED);
        _claim(rando, ID1);
        assertEq(_bal(address(vault)), 0);
    }

    function test_Claim_AfterArbiterReleaseRevertsInvalidStatus() public {
        uint256 dl = _disputed(ID1);
        vm.warp(dl - 1);
        _resolve(ID1, false);
        vm.warp(dl + 30 days);
        _expectInvalidStatus(ID1, RELEASED, DISPUTED);
        _claim(rando, ID1);
        assertEq(vault.poolBalance(), AMT, "released funds stay in the pool");
        assertEq(_bal(address(vault)), AMT);
    }

    function test_Claim_OnlyAffectsTheClaimedId() public {
        uint256 dl1 = _disputed(ID1);
        uint256 dl2 = _disputed(ID2);
        assertGt(dl2, dl1);
        vm.warp(dl1);
        _claim(rando, ID1);
        assertEq(_st(ID2), uint8(DISPUTED));
        assertEq(vault.heldTotal(), AMT);
        _expectTooEarly(dl2);
        _claim(rando, ID2);
        _resolve(ID2, false); // ID2 still within its own window
        assertEq(_st(ID2), uint8(RELEASED));
    }
}
