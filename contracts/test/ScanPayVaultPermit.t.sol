// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {MockPermitToken} from "./mocks/MockPermitToken.sol";
import {MockNoReturnToken} from "./mocks/MockNoReturnToken.sol";
import {MockFeeToken} from "./mocks/MockFeeToken.sol";
import {MockBlocklistToken} from "./mocks/MockBlocklistToken.sol";
import {ScanPayVaultBase} from "./ScanPayVaultBase.t.sol";

/// @dev Real EIP-2612 permits, token-domain variants, decimals, and non-standard token behaviours.
contract ScanPayVaultPermitTest is ScanPayVaultBase {
    uint256 internal constant AMT = 50 * UNIT;

    /// @dev Re-points the fixture at a different token (fresh vault) and funds user + funder.
    function _use(address t) internal {
        _deployVault(t);
        _mint(user, 10_000 * UNIT);
        _mint(funder, 10_000 * UNIT);
    }

    function _fullFlow() internal {
        // offramp -> complete -> reverse ; offramp -> refund ; fund -> onramp -> withdraw ; offramp -> reclaim
        _lock(ID1, AMT);
        skip(15);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        uint256 userBefore = _bal(user);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(_bal(user), userBefore + AMT);

        _lock(ID2, AMT);
        userBefore = _bal(user);
        vm.prank(settler);
        vault.refundOfframp(ID2, 2);
        assertEq(_bal(user), userBefore + AMT);

        _fund(200 * UNIT);
        vm.prank(settler);
        vault.onramp(keccak256("ord"), wallet, 80 * UNIT, 1);
        assertEq(_bal(wallet), 80 * UNIT);
        vm.prank(owner);
        vault.withdrawPool(owner, 120 * UNIT);
        assertEq(_bal(owner), 120 * UNIT);
        assertEq(vault.poolBalance(), 0);

        _lock(ID3, AMT);
        skip(24 hours);
        userBefore = _bal(user);
        vm.prank(user);
        vault.reclaimOfframp(ID3);
        assertEq(_bal(user), userBefore + AMT);
        assertEq(_bal(address(vault)), 0);
        _assertAccounting();
    }

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
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, 100e18 + 1));
        vault.offramp(it, sig, ps);

        skip(15);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(vault.poolBalance(), 100e18);
        vm.prank(settler);
        vault.onramp(keccak256("o"), wallet, 60e18, 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 40e18, 100e18));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        _approve(funder, address(vault), 60e18);
        vm.prank(funder);
        vault.fundPool(60e18);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REVERSED));
        _assertAccounting();
    }

    // ------------------------------------------------------------ permit behaviours
    function test_Permit_FrontRunPermitStillSucceeds() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        // attacker consumes the permit signature directly on the token
        vm.prank(rando);
        token.permit(user, address(vault), AMT, ps.deadline, ps.v, ps.r, ps.s);
        assertEq(token.allowance(user, address(vault)), AMT);
        vm.prank(relayer);
        vault.offramp(it, sig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
        assertEq(vault.escrowedTotal(), AMT);
    }

    function test_Permit_WrongSpenderReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, rando, AMT, block.timestamp + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
        assertEq(vault.escrowedTotal(), 0);
    }

    function test_Permit_SmallerValueReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), AMT - 1, block.timestamp + 1 hours);
        // vault submits permit(value = intent.amount); the signed value differs so the permit is invalid
        // (caught) and no allowance results.
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, ps);
    }

    function test_Permit_LargerValueAlsoInvalidBecauseVaultUsesIntentAmount() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), AMT + 7, block.timestamp + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, ps);
    }

    function test_Permit_SmallerPermitPlusPartialExistingAllowanceStillInsufficient() public {
        _approve(user, address(vault), AMT - 1);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), 1, block.timestamp + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, AMT - 1, AMT));
        vault.offramp(it, sig, ps);
    }

    function test_Permit_ExpiredDeadlineNoAllowanceReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), AMT, block.timestamp - 1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, ps);
    }

    function test_Permit_ExpiredPermitButExistingAllowanceSucceeds() public {
        _approve(user, address(vault), AMT);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), AMT, block.timestamp - 1);
        vault.offramp(it, sig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
    }

    function test_Permit_PermitSignedByOtherKeyNoAllowanceReverts() public {
        (, uint256 otherPk) = makeAddrAndKey("other");
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, otherPk, address(vault), AMT, block.timestamp + 1 hours);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, ps);
    }

    function test_Permit_GarbagePermitWithSufficientApprovalSucceeds() public {
        _approve(user, address(vault), AMT);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        vault.offramp(it, sig, _emptyPermit());
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
        assertEq(_bal(address(vault)), AMT);
    }

    function test_Permit_GarbagePermitWithInsufficientApprovalReverts() public {
        _approve(user, address(vault), AMT - 1);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, AMT - 1, AMT));
        vault.offramp(it, sig, _emptyPermit());
    }

    function test_Permit_ReplayedPermitFailsButAllowanceUsedOnce() public {
        _lock(ID1, AMT);
        // a replay of the same (already consumed) permit cannot create a new allowance
        ScanPayVault.OfframpIntent memory it = _intent(ID2, user, AMT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), AMT, block.timestamp + 1 hours);
        vault.offramp(it, sig, ps); // nonce 1 signed freshly: fine
        ScanPayVault.OfframpIntent memory it3 = _intent(ID3, user, AMT, QR);
        bytes memory sig3 = _signIntent(vault, userPk, it3);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it3, sig3, ps); // stale nonce
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
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.ReceivedAmountMismatch.selector, 100 * UNIT, 0, 99 * UNIT));
        vault.offramp(it, sig, ps);
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
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, user));
        vault.offramp(it, sig, ps);
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

        // reclaim reverts too, then recovers once unblocked
        skip(24 hours);
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, user));
        vm.prank(user);
        vault.reclaimOfframp(ID1);
        t.setBlocked(user, false);
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.REFUNDED));
        assertEq(_bal(address(vault)), 0);
    }

    function test_Blocklist_CompleteStillWorksForBlockedUser() public {
        MockBlocklistToken t = _useBlocklist();
        _lock(ID1, AMT);
        t.setBlocked(user, true);
        skip(15);
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));
        assertEq(vault.poolBalance(), AMT);
        // reverse to the blocked user reverts atomically
        vm.expectRevert(abi.encodeWithSelector(MockBlocklistToken.BlockedAddress.selector, user));
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.COMPLETED));
        assertEq(vault.poolBalance(), AMT);
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
