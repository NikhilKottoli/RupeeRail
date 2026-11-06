// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultUnitBase} from "./ScanPayVaultUnitBase.t.sol";

/// @dev Role setters (settler, quoteSigner, arbiter, guardian) and guardian/pause semantics.
contract ScanPayVaultRolesTest is ScanPayVaultUnitBase {
    // ------------------------------------------------------------ settler
    function test_SetSettler_UpdatesEmitsAndOldLosesPower() public {
        address newSettler = makeAddr("newSettler");
        vm.expectEmit(true, true, false, false, address(vault));
        emit ScanPayVault.SettlerUpdated(settler, newSettler);
        vm.prank(owner);
        vault.setSettler(newSettler);
        assertEq(vault.settler(), newSettler);

        _lock(ID1, UNIT);
        skip(15);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedSettler.selector, settler));
        vm.prank(settler);
        vault.completeOfframp(ID1, REF);
        vm.prank(newSettler);
        vault.completeOfframp(ID1, REF);
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_SetSettler_ZeroRevertsAndOwnerOnly() public {
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        vm.prank(owner);
        vault.setSettler(address(0));
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, settler));
        vm.prank(settler);
        vault.setSettler(rando);
        assertEq(vault.settler(), settler);
    }

    // ------------------------------------------------------------ quote signer
    function test_SetQuoteSigner_UpdatesEmitsAndSwapsWhoseQuotesWork() public {
        (address newSigner, uint256 newPk) = makeAddrAndKey("newQuoteSigner");
        vm.expectEmit(true, true, false, false, address(vault));
        emit ScanPayVault.QuoteSignerUpdated(quoteSigner, newSigner);
        vm.prank(owner);
        vault.setQuoteSigner(newSigner);
        assertEq(vault.quoteSigner(), newSigner);

        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        bytes memory sig = _signIntent(vault, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), UNIT, block.timestamp + 1 hours);
        bytes memory oldQuote = _signQuote(vault, quotePk, it);
        bytes memory newQuote = _signQuote(vault, newPk, it);
        // old signer's quote fails
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, oldQuote, ps);
        // new signer's quote works
        vm.prank(relayer);
        vault.offramp(it, sig, newQuote, ps);
        assertEq(_st(ID1), uint8(LOCKED));
    }

    function test_SetQuoteSigner_ZeroRevertsAndOwnerOnly() public {
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        vm.prank(owner);
        vault.setQuoteSigner(address(0));
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, rando));
        vm.prank(rando);
        vault.setQuoteSigner(rando);
        assertEq(vault.quoteSigner(), quoteSigner);
    }

    // ------------------------------------------------------------ arbiter
    function test_SetArbiter_UpdatesEmitsAndOldLosesPower() public {
        address newArbiter = makeAddr("newArbiter");
        _lockCompleteDispute(ID1, AMT);
        vm.expectEmit(true, true, false, false, address(vault));
        emit ScanPayVault.ArbiterUpdated(arbiter, newArbiter);
        vm.prank(owner);
        vault.setArbiter(newArbiter);
        assertEq(vault.arbiter(), newArbiter);

        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedArbiter.selector, arbiter));
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        vm.prank(newArbiter);
        vault.resolveDispute(ID1, true);
        assertEq(_st(ID1), uint8(REVERSED));
    }

    function test_SetArbiter_ZeroRevertsAndOwnerOnly() public {
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        vm.prank(owner);
        vault.setArbiter(address(0));
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, arbiter));
        vm.prank(arbiter);
        vault.setArbiter(rando);
        assertEq(vault.arbiter(), arbiter);
    }

    // ------------------------------------------------------------ guardian setter
    function test_SetGuardian_UpdatesAndEmits() public {
        vm.expectEmit(true, true, false, false, address(vault));
        emit ScanPayVault.GuardianUpdated(address(0), guardian);
        vm.prank(owner);
        vault.setGuardian(guardian);
        assertEq(vault.guardian(), guardian);

        address g2 = makeAddr("guardian2");
        vm.expectEmit(true, true, false, false, address(vault));
        emit ScanPayVault.GuardianUpdated(guardian, g2);
        vm.prank(owner);
        vault.setGuardian(g2);
        assertEq(vault.guardian(), g2);
    }

    function test_SetGuardian_ZeroAllowedAndDisablesOldGuardian() public {
        vm.startPrank(owner);
        vault.setGuardian(guardian);
        vm.expectEmit(true, true, false, false, address(vault));
        emit ScanPayVault.GuardianUpdated(guardian, address(0));
        vault.setGuardian(address(0));
        vm.stopPrank();
        assertEq(vault.guardian(), address(0));
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedGuardian.selector, guardian));
        vm.prank(guardian);
        vault.guardianPause();
    }

    function test_SetGuardian_OnlyOwner() public {
        address[4] memory callers = [settler, guardian, arbiter, rando];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, callers[i]));
            vm.prank(callers[i]);
            vault.setGuardian(callers[i]);
        }
        assertEq(vault.guardian(), address(0));
    }

    // ------------------------------------------------------------ guardianPause
    function _setGuardian() internal {
        vm.prank(owner);
        vault.setGuardian(guardian);
    }

    function test_Guardian_CanPauseAndEmitsPaused() public {
        _setGuardian();
        vm.expectEmit(false, false, false, true, address(vault));
        emit Pausable.Paused(guardian);
        vm.prank(guardian);
        vault.guardianPause();
        assertTrue(vault.paused());
    }

    function test_Guardian_PauseBlocksOfframpAndOnramp() public {
        _setGuardian();
        _fund(10 * UNIT);
        vm.prank(guardian);
        vault.guardianPause();
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vault.offramp(it, sig, qsig, ps);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vm.prank(settler);
        vault.onramp(keccak256("o"), wallet, UNIT, 1);
    }

    function test_Guardian_PauseIsIdempotentWhenAlreadyPaused() public {
        _setGuardian();
        vm.startPrank(guardian);
        vault.guardianPause();
        vault.guardianPause();
        vm.stopPrank();
        assertTrue(vault.paused());
        _pause(); // owner pausing an already-paused vault is a no-op too
        assertTrue(vault.paused());
    }

    function test_Guardian_CannotUnpause() public {
        _setGuardian();
        vm.prank(guardian);
        vault.guardianPause();
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, guardian));
        vm.prank(guardian);
        vault.setPaused(false);
        assertTrue(vault.paused());
        // there is no guardian unpause entry point
        (bool ok,) = address(vault).call(abi.encodeWithSignature("guardianUnpause()"));
        assertFalse(ok);
        assertTrue(vault.paused());
    }

    function test_Guardian_OnlyOwnerUnpauses() public {
        _setGuardian();
        vm.prank(guardian);
        vault.guardianPause();
        address[3] memory callers = [settler, rando, arbiter];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, callers[i]));
            vm.prank(callers[i]);
            vault.setPaused(false);
        }
        vm.prank(owner);
        vault.setPaused(false);
        assertFalse(vault.paused());
        // guardian can pause again afterwards
        vm.prank(guardian);
        vault.guardianPause();
        assertTrue(vault.paused());
    }

    function test_Guardian_NonGuardianCallersRevert() public {
        _setGuardian();
        address[5] memory callers = [owner, settler, rando, arbiter, user];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedGuardian.selector, callers[i]));
            vm.prank(callers[i]);
            vault.guardianPause();
        }
        assertFalse(vault.paused());
    }

    function test_Guardian_UnsetGuardianAnyCallerReverts() public {
        assertEq(vault.guardian(), address(0));
        address[6] memory callers = [owner, settler, rando, arbiter, user, guardian];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedGuardian.selector, callers[i]));
            vm.prank(callers[i]);
            vault.guardianPause();
        }
        assertFalse(vault.paused());
    }

    function test_Guardian_HasNoOtherPowers() public {
        _setGuardian();
        bytes[6] memory calls = [
            abi.encodeCall(vault.setPaused, (false)),
            abi.encodeCall(vault.setSettler, (guardian)),
            abi.encodeCall(vault.setArbiter, (guardian)),
            abi.encodeCall(vault.setQuoteSigner, (guardian)),
            abi.encodeCall(vault.withdrawPool, (guardian, 0)),
            abi.encodeCall(vault.sweepSurplus, (guardian))
        ];
        for (uint256 i; i < calls.length; i++) {
            vm.prank(guardian);
            (bool ok, bytes memory ret) = address(vault).call(calls[i]);
            assertFalse(ok);
            assertEq(ret, abi.encodeWithSelector(Ownable.OwnableUnauthorizedAccount.selector, guardian));
        }
    }
}
