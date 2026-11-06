// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {MockPermitToken} from "./mocks/MockPermitToken.sol";
import {MockNoReturnToken} from "./mocks/MockNoReturnToken.sol";
import {MockFeeToken} from "./mocks/MockFeeToken.sol";
import {MockBlocklistToken} from "./mocks/MockBlocklistToken.sol";
import {ScanPayVaultPermitBase} from "./ScanPayVaultPermitBase.t.sol";

/// @dev Real EIP-2612 permits, token-domain variants, decimals, and non-standard token behaviours.
contract ScanPayVaultPermitTest is ScanPayVaultPermitBase {
    // ------------------------------------------------------------ token domain variants
    function test_Permit_Usdc6dV2FullFlow() public {
        assertEq(vault.decimals(), 6);
        _fullFlow();
    }

    function test_Permit_Tether6dV1FullFlow() public {
        MockPermitToken t = new MockPermitToken("Tether USD", "1", 6);
        assertTrue(t.DOMAIN_SEPARATOR() != token.DOMAIN_SEPARATOR());
        _use(address(t));
        assertEq(vault.decimals(), 6);
        _fullFlow();
    }

    function test_Permit_18DecimalsFullFlowAndScaledDefaults() public {
        MockPermitToken t = new MockPermitToken("Dai-like", "1", 18);
        _deployVault(address(t));
        _mint(user, 10_000e18);
        _mint(funder, 10_000e18);
        assertEq(vault.decimals(), 18);
        assertEq(vault.maxOfframpAmount(), 100e18);
        assertEq(vault.dailyEmitCap(), 1000e18);

        _lock(ID1, 100e18);
        // 100e18 + 1 exceeds max
        ScanPayVault.OfframpIntent memory it = _intent(ID2, user, 100e18 + 1, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, 100e18 + 1));
        vault.offramp(it, sig, qsig, ps);

        skip(15);
        _completeWith(ID1);
        // completion moves escrow -> held, NOT into the pool
        assertEq(vault.heldTotal(), 100e18);
        assertEq(vault.poolBalance(), 0);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, 60e18));
        vm.prank(settler);
        vault.onramp(keccak256("o"), wallet, 60e18, 1);

        _approve(funder, address(vault), 60e18);
        vm.prank(funder);
        vault.fundPool(60e18);
        vm.prank(settler);
        vault.onramp(keccak256("o"), wallet, 60e18, 1);
        assertEq(_bal(wallet), 60e18);

        // reversal draws held funds only, so it works even with the pool fully drained
        assertEq(vault.poolBalance(), 0);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REVERSED));
        assertEq(vault.heldTotal(), 0);

        // complete -> held -> release -> pool at 18 decimals
        _lockCompleteRelease(ID3, 10e18);
        assertEq(vault.poolBalance(), 10e18);
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    // ------------------------------------------------------------ permit behaviours
    function test_Permit_FrontRunPermitStillSucceeds() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        // attacker consumes the permit signature directly on the token
        vm.prank(rando);
        token.permit(user, address(vault), AMT, ps.deadline, ps.v, ps.r, ps.s);
        assertEq(token.allowance(user, address(vault)), AMT);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
        assertEq(vault.escrowedTotal(), AMT);
    }

    function test_Permit_WrongSpenderReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, rando, AMT, block.timestamp + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, qsig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
        assertEq(vault.escrowedTotal(), 0);
    }

    function test_Permit_SmallerValueReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        ScanPayVault.PermitSig memory ps =
            _signPermit(tokAddr, userPk, address(vault), AMT - 1, block.timestamp + 1 hours);
        // vault submits permit(value = intent.amount); the signed value differs so the permit is invalid
        // (caught) and no allowance results.
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Permit_LargerValueAlsoInvalidBecauseVaultUsesIntentAmount() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        ScanPayVault.PermitSig memory ps =
            _signPermit(tokAddr, userPk, address(vault), AMT + 7, block.timestamp + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Permit_SmallerPermitPlusPartialExistingAllowanceStillInsufficient() public {
        _approve(user, address(vault), AMT - 1);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), 1, block.timestamp + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, AMT - 1, AMT));
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Permit_ExpiredDeadlineNoAllowanceReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), AMT, block.timestamp - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Permit_ExpiredPermitButExistingAllowanceSucceeds() public {
        _approve(user, address(vault), AMT);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), AMT, block.timestamp - 1);
        vault.offramp(it, sig, qsig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
    }

    function test_Permit_PermitSignedByOtherKeyNoAllowanceReverts() public {
        (, uint256 otherPk) = makeAddrAndKey("other");
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        ScanPayVault.PermitSig memory ps =
            _signPermit(tokAddr, otherPk, address(vault), AMT, block.timestamp + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Permit_GarbagePermitWithSufficientApprovalSucceeds() public {
        _approve(user, address(vault), AMT);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        vault.offramp(it, sig, qsig, _emptyPermit());
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
        assertEq(_bal(address(vault)), AMT);
    }

    function test_Permit_GarbagePermitWithInsufficientApprovalReverts() public {
        _approve(user, address(vault), AMT - 1);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, AMT - 1, AMT));
        vault.offramp(it, sig, qsig, _emptyPermit());
    }

    function test_Permit_ReplayedPermitFailsButAllowanceUsedOnce() public {
        _lock(ID1, AMT);
        // a fresh permit (nonce 1) for the second lock works and is consumed
        ScanPayVault.OfframpIntent memory it = _intent(ID2, user, AMT, QR);
        (bytes memory sig, bytes memory qsig) = _sigs(it);
        ScanPayVault.PermitSig memory ps =
            _signPermit(tokAddr, userPk, address(vault), AMT, block.timestamp + 1 hours);
        vault.offramp(it, sig, qsig, ps);
        // replaying the same permit signature (stale nonce) cannot create a new allowance
        ScanPayVault.OfframpIntent memory it3 = _intent(ID3, user, AMT, QR);
        (bytes memory sig3, bytes memory qsig3) = _sigs(it3);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it3, sig3, qsig3, ps);
    }

    // ------------------------------------------------------------ USDT-style no-return token
    function test_NoReturnToken_FullFlowViaSafeERC20() public {
        MockNoReturnToken t = new MockNoReturnToken("Tether USD", "1");
        _use(address(t));
        _fullFlow();
    }

    // ------------------------------------------------------------ fee-on-transfer token
    function test_FeeToken_OfframpRevertsReceivedAmountMismatch() public {
        MockFeeToken t = new MockFeeToken("USDC", "2");
        _use(address(t));
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 100 * UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.ReceivedAmountMismatch.selector, 100 * UNIT, 0, 99 * UNIT));
        vault.offramp(it, sig, qsig, ps);
        assertEq(vault.escrowedTotal(), 0);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
    }

    function test_FeeToken_FundPoolRevertsReceivedAmountMismatch() public {
        MockFeeToken t = new MockFeeToken("USDC", "2");
        _use(address(t));
        _approve(funder, address(vault), 100 * UNIT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.ReceivedAmountMismatch.selector, 100 * UNIT, 0, 99 * UNIT));
        vm.prank(funder);
        vault.fundPool(100 * UNIT);
        assertEq(vault.poolBalance(), 0);
    }

    function test_FeeToken_ZeroFeeBehavesNormally() public {
        MockFeeToken t = new MockFeeToken("USDC", "2");
        _use(address(t));
        t.setFeeBps(0);
        _lock(ID1, AMT);
        assertEq(vault.escrowedTotal(), AMT);
        assertEq(_bal(address(vault)), AMT);
    }

    // ------------------------------------------------------------ blocklisting token
    function _useBlocklist() internal returns (MockBlocklistToken t) {
        t = new MockBlocklistToken("USDC", "2");
        _use(address(t));
    }

    function test_Blocklist_BlockedUserOfframpReverts() public {
        MockBlocklistToken t = _useBlocklist();
        t.setBlocked(user, true);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, user));
        vault.offramp(it, sig, qsig, ps);
        assertEq(vault.escrowedTotal(), 0);
    }

    function test_Blocklist_BlockedAfterLockRefundRevertsStatusStaysLocked() public {
        MockBlocklistToken t = _useBlocklist();
        _lock(ID1, AMT);
        t.setBlocked(user, true);
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, user));
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
        assertEq(vault.escrowedTotal(), AMT);
        assertEq(_bal(address(vault)), AMT);
        _assertAccounting();

        // reclaim (by anyone) reverts too, then recovers once unblocked
        skip(24 hours);
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, user));
        vm.prank(rando);
        vault.reclaimOfframp(ID1);
        t.setBlocked(user, false);
        vm.prank(rando);
        vault.reclaimOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REFUNDED));
        assertEq(_bal(address(vault)), 0);
    }

    function test_Blocklist_CompleteStillWorksForBlockedUser() public {
        MockBlocklistToken t = _useBlocklist();
        _lock(ID1, AMT);
        t.setBlocked(user, true);
        skip(15);
        _completeWith(ID1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));
        assertEq(vault.heldTotal(), AMT);
        assertEq(vault.poolBalance(), 0);
        // reverse to the blocked user reverts atomically
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, user));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));
        assertEq(vault.heldTotal(), AMT);
        // release moves no tokens, so a blocked user cannot stall it
        vm.warp(_dl(ID1).releasableAt);
        vault.releaseOfframp(ID1);
        assertEq(vault.poolBalance(), AMT);
        assertEq(vault.heldTotal(), 0);
    }

    function test_Blocklist_BlockedDisputeRefundRevertsButOthersUnaffected() public {
        MockBlocklistToken t = _useBlocklist();
        _lockComplete(ID1, AMT);
        vm.prank(user);
        vault.disputeOfframp(ID1);
        t.setBlocked(user, true);
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, user));
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.DISPUTED));
        assertEq(vault.heldTotal(), AMT);
        // an unrelated user's lock/refund still works while this user is blocklisted
        (address other, uint256 otherPk) = makeAddrAndKey("otherUser");
        _mint(other, AMT);
        _relay(_intent(ID2, other, AMT, QR), otherPk, otherPk);
        vm.prank(settler);
        vault.refundOfframp(ID2, 1);
        assertEq(_bal(other), AMT);
        // the pool-side resolution does not transfer, so it is not blocked
        vm.prank(arbiter);
        vault.resolveDispute(ID1, false);
        assertEq(vault.poolBalance(), AMT);
    }

    /// @dev Accepted limitation: a blocklisted user's timeout refund reverts atomically (status/heldTotal
    /// intact) and recovers once unblocked.
    function test_Blocklist_BlockedUserDisputeTimeoutClaimRevertsThenRecovers() public {
        MockBlocklistToken t = _useBlocklist();
        _lockComplete(ID1, AMT);
        vm.prank(user);
        vault.disputeOfframp(ID1);
        uint256 dl = _dl(ID1).releasableAt;
        t.setBlocked(user, true);
        vm.warp(dl);
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, user));
        vm.prank(rando);
        vault.claimDisputeTimeout(ID1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.DISPUTED));
        assertEq(vault.heldTotal(), AMT);
        assertEq(_bal(address(vault)), AMT);
        _assertAccounting();
        // the arbiter's window is closed too, so the funds wait for the unblock
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.DisputeResolutionExpired.selector, dl));
        vm.prank(arbiter);
        vault.resolveDispute(ID1, false);

        uint256 before = _bal(user);
        t.setBlocked(user, false);
        vm.prank(rando);
        vault.claimDisputeTimeout(ID1);
        assertEq(_bal(user), before + AMT);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REVERSED));
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function test_Blocklist_BlockedOnrampRecipientRevertsWithoutConsumingOrderOrCap() public {
        MockBlocklistToken t = _useBlocklist();
        _fund(100 * UNIT);
        t.setBlocked(wallet, true);
        bytes32 ord = keccak256("ord");
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, wallet));
        vm.prank(settler);
        vault.onramp(ord, wallet, 10 * UNIT, 1);
        assertFalse(vault.onrampDone(ord));
        assertEq(vault.emittedInWindow(), 0);
        assertEq(vault.remainingDailyCap(), 1000 * UNIT);
        assertEq(vault.poolBalance(), 100 * UNIT);

        t.setBlocked(wallet, false);
        vm.prank(settler);
        vault.onramp(ord, wallet, 10 * UNIT, 1);
        assertTrue(vault.onrampDone(ord));
        assertEq(vault.emittedInWindow(), 10 * UNIT);
    }

    function test_Blocklist_BlockedFunderReverts() public {
        MockBlocklistToken t = _useBlocklist();
        _approve(funder, address(vault), UNIT);
        t.setBlocked(funder, true);
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, funder));
        vm.prank(funder);
        vault.fundPool(UNIT);
        assertEq(vault.poolBalance(), 0);
    }
}
