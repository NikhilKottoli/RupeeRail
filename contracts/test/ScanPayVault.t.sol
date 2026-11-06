// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {MockPermitToken} from "./mocks/MockPermitToken.sol";
import {ScanPayVaultUnitBase} from "./ScanPayVaultUnitBase.t.sol";

/// @dev Constructor, hashing and offramp (lock) unit tests.
contract ScanPayVaultTest is ScanPayVaultUnitBase {
    // ------------------------------------------------------------ constructor
    function test_Constructor_Defaults() public view {
        assertEq(address(vault.token()), address(token));
        assertEq(vault.decimals(), 6);
        assertEq(vault.owner(), owner);
        assertEq(vault.settler(), settler);
        assertEq(vault.quoteSigner(), quoteSigner);
        assertEq(vault.arbiter(), arbiter);
        assertEq(vault.guardian(), address(0), "guardian starts unset");
        assertEq(vault.maxOfframpAmount(), 100 * UNIT);
        assertEq(vault.dailyEmitCap(), 1000 * UNIT);
        assertEq(vault.completeDelay(), 15);
        assertEq(vault.disputeWindow(), 24 hours);
        assertEq(vault.reclaimDelay(), 24 hours);
        assertEq(vault.MIN_DISPUTE_WINDOW(), 1 hours);
        assertEq(vault.disputeTimeout(), 7 days);
        assertEq(vault.MIN_DISPUTE_TIMEOUT(), 1 days);
        assertEq(vault.MAX_DISPUTE_TIMEOUT(), 30 days);
        assertEq(vault.emitWindowStart(), T0);
        assertEq(vault.emittedInWindow(), 0);
        assertEq(vault.escrowedTotal(), 0);
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 0);
        assertFalse(vault.paused());
    }

    function test_Constructor_EachZeroArgReverts() public {
        IERC20 t = IERC20(address(token));
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        new ScanPayVault(IERC20(address(0)), owner, settler, quoteSigner, arbiter);
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        new ScanPayVault(t, address(0), settler, quoteSigner, arbiter);
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        new ScanPayVault(t, owner, address(0), quoteSigner, arbiter);
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        new ScanPayVault(t, owner, settler, address(0), arbiter);
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        new ScanPayVault(t, owner, settler, quoteSigner, address(0));
    }

    function test_Constructor_NonContractTokenReverts() public {
        vm.expectRevert();
        new ScanPayVault(IERC20(makeAddr("eoa")), owner, settler, quoteSigner, arbiter);
    }

    function test_Constructor_UnsupportedDecimals() public {
        MockPermitToken t27 = new MockPermitToken("X", "1", 27);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnsupportedDecimals.selector, uint8(27)));
        new ScanPayVault(IERC20(address(t27)), owner, settler, quoteSigner, arbiter);
    }

    function test_Constructor_18DecimalsScalesDefaults() public {
        MockPermitToken t18 = new MockPermitToken("X", "1", 18);
        ScanPayVault v = new ScanPayVault(IERC20(address(t18)), owner, settler, quoteSigner, arbiter);
        assertEq(v.decimals(), 18);
        assertEq(v.maxOfframpAmount(), 100e18);
        assertEq(v.dailyEmitCap(), 1000e18);
    }

    function test_Constructor_26DecimalsOk() public {
        MockPermitToken t26 = new MockPermitToken("X", "1", 26);
        ScanPayVault v = new ScanPayVault(IERC20(address(t26)), owner, settler, quoteSigner, arbiter);
        assertEq(v.maxOfframpAmount(), 100 * 1e26);
        assertLe(v.maxOfframpAmount(), type(uint96).max);
    }

    // ------------------------------------------------------------ hashIntent
    function test_HashIntent_MatchesManualEip712() public view {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        bytes32 typeHash = keccak256(
            "OfframpIntent(bytes32 id,address user,uint256 amount,uint64 inrPaise,uint64 rateE6,bytes32 qrCommitment,uint64 deadline)"
        );
        bytes32 structHash = keccak256(
            abi.encode(typeHash, it.id, it.user, it.amount, it.inrPaise, it.rateE6, it.qrCommitment, it.deadline)
        );
        bytes32 domain = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256("ScanPayVault"),
                keccak256("1"),
                block.chainid,
                address(vault)
            )
        );
        assertEq(vault.hashIntent(it), keccak256(abi.encodePacked("\x19\x01", domain, structHash)));
        assertEq(vault.OFFRAMP_INTENT_TYPEHASH(), typeHash);
        assertEq(it.qrCommitment, keccak256(abi.encode(SALT, QR)));
    }

    function test_Offramp_SignaturesBoundToVault() public {
        ScanPayVault other = new ScanPayVault(IERC20(address(token)), owner, settler, quoteSigner, arbiter);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        bytes memory sigOther = _signIntent(other, userPk, it);
        bytes memory qsig = _signQuote(vault, quotePk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), it.amount, block.timestamp + 1 hours);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vm.prank(relayer);
        vault.offramp(it, sigOther, qsig, ps);

        // a valid user sig but a quote signed for another vault
        bytes memory sig = _signIntent(vault, userPk, it);
        bytes memory qsigOther = _signQuote(other, quotePk, it);
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, qsigOther, ps);
    }

    // ------------------------------------------------------------ offramp happy
    function test_Offramp_HappyPath() public {
        uint256 amt = 50 * UNIT;
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amt, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        uint256 userBefore = _bal(user);

        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpLocked(ID1, user, amt, it.inrPaise, it.rateE6, it.qrCommitment);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);

        Off memory o = _off(ID1);
        assertEq(o.user, user);
        assertEq(o.amount, amt);
        assertEq(o.inrPaise, it.inrPaise);
        assertEq(o.lockedAt, T0);
        assertEq(o.completedAt, 0);
        assertEq(uint8(o.status), uint8(LOCKED));
        assertEq(o.qrCommitment, it.qrCommitment);

        Dl memory d = _dl(ID1);
        assertEq(d.completableAt, T0 + 15);
        assertEq(d.reclaimableAt, T0 + 24 hours);
        assertEq(d.disputeWindow, 24 hours);
        assertEq(d.releasableAt, 0);

        assertEq(vault.escrowedTotal(), amt);
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 0);
        assertEq(_bal(user), userBefore - amt);
        assertEq(_bal(address(vault)), amt);
        assertEq(_bal(relayer), 0);
        assertEq(token.allowance(user, address(vault)), 0);
        _assertAccounting();
    }

    function test_Offramp_AnyoneCanRelay() public {
        address[4] memory relayers = [relayer, rando, settler, owner];
        for (uint256 i; i < relayers.length; i++) {
            ScanPayVault.OfframpIntent memory it = _intent(bytes32(i + 1), user, UNIT, QR);
            (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
            vm.prank(relayers[i]);
            vault.offramp(it, sig, qsig, ps);
            assertEq(_st(bytes32(i + 1)), uint8(LOCKED));
        }
        assertEq(vault.escrowedTotal(), 4 * UNIT);
    }

    function test_Offramp_ValidAtExactlyDeadline() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        vm.warp(it.deadline);
        _relay(it, userPk, userPk);
        assertEq(_st(ID1), uint8(LOCKED));
    }

    function test_Offramp_MaxAmountAllowed() public {
        _lock(ID1, 100 * UNIT);
        assertEq(vault.escrowedTotal(), 100 * UNIT);
    }

    function test_Offramp_Uint96MaxAmountAllowed() public {
        uint256 m = type(uint96).max;
        vm.prank(owner);
        vault.setMaxOfframpAmount(m);
        _mint(user, m);
        _lock(ID1, m);
        assertEq(_off(ID1).amount, m);
    }

    function test_Offramp_OneBaseUnitAllowed() public {
        _lock(ID1, 1);
        assertEq(_off(ID1).amount, 1);
    }

    // ------------------------------------------------------------ offramp reverts
    function test_Offramp_WrongUserSignerReverts() public {
        (, uint256 otherPk) = makeAddrAndKey("other");
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, otherPk, userPk);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_EmptyOrShortUserSignatureReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        bytes memory qsig = _signQuote(vault, quotePk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), UNIT, block.timestamp + 1 hours);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, "", qsig, ps);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, new bytes(64), qsig, ps);
    }

    function test_Offramp_ExpiredReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        vm.warp(uint256(it.deadline) + 1);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.IntentExpired.selector, it.deadline));
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_TamperedFieldsFailUserSignature() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        it.amount = 6 * UNIT;
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, qsig, ps);
        it.amount = 5 * UNIT;
        it.inrPaise += 1;
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, qsig, ps);
        it.inrPaise -= 1;
        it.rateE6 += 1;
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, qsig, ps);
        it.rateE6 -= 1;
        it.deadline += 1;
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, qsig, ps);
        it.deadline -= 1;
        it.id = ID2;
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_ZeroAmountReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 0, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidAmount.selector, 0));
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_AboveMaxReverts() public {
        uint256 amt = 100 * UNIT + 1;
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amt, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, amt));
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_MaxZeroDisablesOfframp() public {
        vm.prank(owner);
        vault.setMaxOfframpAmount(0);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 1, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, 1));
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_AboveUint96Reverts() public {
        vm.prank(owner);
        vault.setMaxOfframpAmount(type(uint96).max);
        uint256 amt = uint256(type(uint96).max) + 1;
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amt, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, amt));
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_ReusedIdAfterEveryTerminalOrActiveStatusReverts() public {
        _lock(ID1, UNIT); // LOCKED
        _expectIdUsedOnRelock(ID1);

        _lockComplete(ID2, UNIT); // COMPLETED
        _expectIdUsedOnRelock(ID2);
        _lockComplete(ID3, UNIT);
        _dispute(ID3); // DISPUTED
        _expectIdUsedOnRelock(ID3);

        bytes32 a = keccak256("a");
        _lock(a, UNIT);
        vm.prank(settler);
        vault.refundOfframp(a, 1); // REFUNDED
        _expectIdUsedOnRelock(a);

        bytes32 b = keccak256("b");
        _lockComplete(b, UNIT);
        vm.prank(settler);
        vault.reverseOfframp(b); // REVERSED
        _expectIdUsedOnRelock(b);

        bytes32 c = keccak256("c");
        _lockCompleteRelease(c, UNIT); // RELEASED
        assertEq(_st(c), uint8(RELEASED));
        _expectIdUsedOnRelock(c);
    }

    function _expectIdUsedOnRelock(bytes32 id) internal {
        ScanPayVault.OfframpIntent memory it = _intent(id, user, UNIT, QR);
        it.deadline = uint64(block.timestamp + 1 hours);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpIdUsed.selector, id));
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_PausedReverts() public {
        _pause();
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_ZeroUserReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, address(0), UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        vault.offramp(it, sig, qsig, ps);
    }

    function test_Offramp_InsufficientUserBalanceRevertsAtomically() public {
        (address poor, uint256 poorPk) = makeAddrAndKey("poor");
        _mint(poor, UNIT - 1);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, poor, UNIT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, poorPk, poorPk);
        vm.expectRevert();
        vault.offramp(it, sig, qsig, ps);
        assertEq(_st(ID1), uint8(NONE));
        assertEq(vault.escrowedTotal(), 0);
    }

    function test_Offramps_UnknownIdGetterIsZero() public view {
        Off memory o = _off(keccak256("unknown"));
        assertEq(o.user, address(0));
        assertEq(uint8(o.status), 0);
        assertEq(o.qrCommitment, bytes32(0));
        Dl memory d = _dl(keccak256("unknown"));
        assertEq(d.reclaimableAt, 0);
    }
}
