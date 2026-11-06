// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {MockPermitToken} from "./mocks/MockPermitToken.sol";
import {ScanPayVaultBase} from "./ScanPayVaultBase.t.sol";

/// @dev Constructor, hashing and offramp (lock) unit tests.
contract ScanPayVaultTest is ScanPayVaultBase {
    // ------------------------------------------------------------ constructor
    function test_Constructor_Defaults() public view {
        assertEq(address(vault.token()), address(token));
        assertEq(vault.decimals(), 6);
        assertEq(vault.owner(), owner);
        assertEq(vault.settler(), settler);
        assertEq(vault.maxOfframpAmount(), 100 * UNIT);
        assertEq(vault.dailyEmitCap(), 1000 * UNIT);
        assertEq(vault.completeDelay(), 15);
        assertEq(vault.disputeWindow(), 24 hours);
        assertEq(vault.reclaimDelay(), 24 hours);
        assertEq(vault.emitWindowStart(), T0);
        assertEq(vault.emittedInWindow(), 0);
        assertEq(vault.escrowedTotal(), 0);
        assertEq(vault.poolBalance(), 0);
        assertFalse(vault.paused());
    }

    function test_Constructor_ZeroTokenReverts() public {
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        new ScanPayVault(IERC20(address(0)), owner, settler);
    }

    function test_Constructor_ZeroOwnerReverts() public {
        vm.expectRevert(abi.encodeWithSelector(Ownable.OwnableInvalidOwner.selector, address(0)));
        new ScanPayVault(IERC20(address(token)), address(0), settler);
    }

    function test_Constructor_ZeroSettlerReverts() public {
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        new ScanPayVault(IERC20(address(token)), owner, address(0));
    }

    function test_Constructor_NonContractTokenReverts() public {
        vm.expectRevert();
        new ScanPayVault(IERC20(makeAddr("eoa")), owner, settler);
    }

    function test_Constructor_UnsupportedDecimals() public {
        MockPermitToken t27 = new MockPermitToken("X", "1", 27);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnsupportedDecimals.selector, uint8(27)));
        new ScanPayVault(IERC20(address(t27)), owner, settler);
    }

    function test_Constructor_18DecimalsScalesDefaults() public {
        MockPermitToken t18 = new MockPermitToken("X", "1", 18);
        ScanPayVault v = new ScanPayVault(IERC20(address(t18)), owner, settler);
        assertEq(v.decimals(), 18);
        assertEq(v.maxOfframpAmount(), 100e18);
        assertEq(v.dailyEmitCap(), 1000e18);
    }

    function test_Constructor_26DecimalsOk() public {
        MockPermitToken t26 = new MockPermitToken("X", "1", 26);
        ScanPayVault v = new ScanPayVault(IERC20(address(t26)), owner, settler);
        assertEq(v.maxOfframpAmount(), 100 * 1e26);
        assertLe(v.maxOfframpAmount(), type(uint96).max);
    }

    // ------------------------------------------------------------ hashIntent
    function test_HashIntent_MatchesManualEip712() public view {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        bytes32 typeHash = keccak256(
            "OfframpIntent(bytes32 id,address user,uint256 amount,uint64 inrPaise,uint64 rateE6,string qrText,uint64 deadline)"
        );
        bytes32 structHash = keccak256(
            abi.encode(typeHash, it.id, it.user, it.amount, it.inrPaise, it.rateE6, keccak256(bytes(QR)), it.deadline)
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
    }

    function test_Offramp_SignatureBoundToVault() public {
        ScanPayVault other = new ScanPayVault(IERC20(address(token)), owner, settler);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        bytes memory sigOther = _signIntent(other, userPk, it);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), it.amount, block.timestamp + 1 hours);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vm.prank(relayer);
        vault.offramp(it, sigOther, ps);
    }

    // ------------------------------------------------------------ offramp happy
    function test_Offramp_HappyPath() public {
        uint256 amt = 50 * UNIT;
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amt, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        uint256 userBefore = _bal(user);

        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpLocked(ID1, user, amt, it.inrPaise, it.rateE6, QR);
        vm.prank(relayer);
        vault.offramp(it, sig, ps);

        Off memory o = _off(ID1);
        assertEq(o.user, user);
        assertEq(o.amount, amt);
        assertEq(o.inrPaise, it.inrPaise);
        assertEq(o.lockedAt, T0);
        assertEq(o.completedAt, 0);
        assertEq(uint8(o.status), uint8(ScanPayVault.Status.LOCKED));
        assertEq(o.qrHash, keccak256(bytes(QR)));

        Dl memory d = _dl(ID1);
        assertEq(d.reclaimableAt, T0 + 24 hours);
        assertEq(d.reversibleUntil, 0);
        assertEq(d.disputeWindow, 24 hours);

        assertEq(vault.escrowedTotal(), amt);
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
            (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
            vm.prank(relayers[i]);
            vault.offramp(it, sig, ps);
            assertEq(uint8(_status(bytes32(i + 1))), uint8(ScanPayVault.Status.LOCKED));
        }
        assertEq(vault.escrowedTotal(), 4 * UNIT);
    }

    function test_Offramp_ValidAtExactlyDeadline() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        vm.warp(it.deadline);
        _relay(it, userPk, userPk);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
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

    // ------------------------------------------------------------ offramp reverts
    function test_Offramp_WrongSignerReverts() public {
        (, uint256 otherPk) = makeAddrAndKey("other");
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, otherPk, userPk);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_EmptyOrShortSignatureReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        ScanPayVault.PermitSig memory ps = _signPermit(tokAddr, userPk, address(vault), UNIT, block.timestamp + 1 hours);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, "", ps);
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, new bytes(64), ps);
    }

    function test_Offramp_ExpiredReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        vm.warp(uint256(it.deadline) + 1);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.IntentExpired.selector, it.deadline));
        vm.prank(relayer);
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_SignatureOverDifferentQrTextFails() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, "upi://pay?pa=honest@upi");
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        it.qrText = "upi://pay?pa=attacker@upi";
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_TamperedFieldsFail() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 5 * UNIT, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        it.amount = 6 * UNIT;
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, ps);
        it.amount = 5 * UNIT;
        it.inrPaise += 1;
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, ps);
        it.inrPaise -= 1;
        it.rateE6 += 1;
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_QrLengthBounds() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, "");
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidQrLength.selector, 0));
        vault.offramp(it, sig, ps);

        it = _intent(ID2, user, UNIT, _qr(513));
        (sig, ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidQrLength.selector, 513));
        vault.offramp(it, sig, ps);

        it = _intent(ID3, user, UNIT, _qr(512));
        _relay(it, userPk, userPk);
        assertEq(_off(ID3).qrHash, keccak256(bytes(_qr(512))));
    }

    function test_Offramp_OneByteQrOk() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, "x");
        _relay(it, userPk, userPk);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
    }

    function test_Offramp_ZeroAmountReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 0, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InvalidAmount.selector, 0));
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_AboveMaxReverts() public {
        uint256 amt = 100 * UNIT + 1;
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amt, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, amt));
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_MaxZeroDisablesOfframp() public {
        vm.prank(owner);
        vault.setMaxOfframpAmount(0);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, 1, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, 1));
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_AboveUint96Reverts() public {
        vm.prank(owner);
        vault.setMaxOfframpAmount(type(uint96).max);
        uint256 amt = uint256(type(uint96).max) + 1;
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, amt, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpAmountTooLarge.selector, amt));
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_ReusedIdAfterLockedReverts() public {
        _lock(ID1, UNIT);
        _expectIdUsedOnRelock();
    }

    function test_Offramp_ReusedIdAfterCompletedReverts() public {
        _lockComplete(ID1, UNIT);
        _expectIdUsedOnRelock();
    }

    function test_Offramp_ReusedIdAfterRefundedReverts() public {
        _lock(ID1, UNIT);
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        _expectIdUsedOnRelock();
    }

    function test_Offramp_ReusedIdAfterReversedReverts() public {
        _lockComplete(ID1, UNIT);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        _expectIdUsedOnRelock();
    }

    function _expectIdUsedOnRelock() internal {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.OfframpIdUsed.selector, ID1));
        vm.prank(relayer);
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_PausedReverts() public {
        vm.prank(owner);
        vault.setPaused(true);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, user, UNIT, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_ZeroUserReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, address(0), UNIT, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(ScanPayVault.ZeroAddress.selector);
        vault.offramp(it, sig, ps);
    }

    function test_Offramp_InsufficientUserBalanceRevertsAtomically() public {
        address poor;
        uint256 poorPk;
        (poor, poorPk) = makeAddrAndKey("poor");
        _mint(poor, UNIT - 1);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, poor, UNIT, QR);
        (bytes memory sig, ScanPayVault.PermitSig memory ps) = _prep(it, poorPk, poorPk);
        vm.expectRevert();
        vault.offramp(it, sig, ps);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
        assertEq(vault.escrowedTotal(), 0);
    }

    function test_Offramps_UnknownIdGetterIsZero() public view {
        // getters for unknown id are all-zero
        Off memory o = _off(keccak256("unknown"));
        assertEq(o.user, address(0));
        assertEq(uint8(o.status), 0);
    }
}
