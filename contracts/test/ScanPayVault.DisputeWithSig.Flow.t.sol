// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../src/ScanPayVault.sol";
import {MockERC1271Wallet} from "./mocks/MockERC1271Wallet.sol";
import {ScanPayVaultDisputeSigBase} from "./ScanPayVault.DisputeWithSig.Base.t.sol";

/// @dev Relayed dispute: ERC-1271 users, the post-dispute lifecycle and the dishonest-settler scenario.
contract ScanPayVaultDisputeWithSigFlowTest is ScanPayVaultDisputeSigBase {
    MockERC1271Wallet internal smart;
    address internal walletSigner;
    uint256 internal walletPk;

    function setUp() public override {
        super.setUp();
        (walletSigner, walletPk) = makeAddrAndKey("walletSigner");
        smart = new MockERC1271Wallet(walletSigner);
        _mint(address(smart), 1_000 * UNIT);
    }

    function _walletLockComplete(bytes32 id) internal {
        vm.prank(walletSigner);
        smart.execute(tokAddr, abi.encodeWithSignature("approve(address,uint256)", address(vault), AMT));
        ScanPayVault.OfframpIntent memory it = _intent(id, address(smart), AMT, QR);
        bytes memory sig = _signIntent(vault, walletPk, it);
        bytes memory qsig = _signQuote(vault, quotePk, it);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, _emptyPermit());
        skip(15);
        _complete(id);
    }

    // ------------------------------------------------------------ (e) ERC-1271 user
    function test_Wallet_DisputesViaRelayerWithWalletSignerSignature() public {
        _walletLockComplete(ID1);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _signDispute(walletPk, ID1, address(smart), d);
        uint64 nonce = vm.getNonce(walletSigner);

        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpDisputed(ID1, address(smart), AMT);
        _relayDispute(ID1, d, sig);

        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(_dl(ID1).releasableAt, block.timestamp + vault.disputeTimeout());
        assertEq(vm.getNonce(walletSigner), nonce, "wallet signer sent no tx");
        assertEq(vault.heldTotal(), AMT);
    }

    function test_Wallet_WrongSignerFailsInvalidSignature() public {
        _walletLockComplete(ID1);
        uint64 d = _deadlineIn(1 hours);
        bytes memory wrong = _signDispute(userPk, ID1, address(smart), d); // not the wallet's configured EOA
        _expectBadSig();
        _relayDispute(ID1, d, wrong);
        bytes memory garbage = hex"1234";
        _expectBadSig();
        _relayDispute(ID1, d, garbage);
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_Wallet_SignatureNamingTheSignerEoaAsUserFails() public {
        _walletLockComplete(ID1);
        uint64 d = _deadlineIn(1 hours);
        // digest names the EOA, not the wallet that owns the offramp
        bytes memory sig = _signDispute(walletPk, ID1, walletSigner, d);
        _expectBadSig();
        _relayDispute(ID1, d, sig);
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_Wallet_DirectDisputeStillNeedsTheWalletAsSender() public {
        _walletLockComplete(ID1);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedUser.selector, walletSigner));
        vm.prank(walletSigner);
        vault.disputeOfframp(ID1);
        vm.prank(walletSigner);
        smart.execute(address(vault), abi.encodeWithSelector(ScanPayVault.disputeOfframp.selector, ID1));
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    // ------------------------------------------------------------ (f) after a relayed dispute
    function test_After_ArbiterRefundsBeforeDeadline() public {
        uint256 dl = _lockCompleteRelayedDispute(ID1);
        vm.warp(dl - 1);
        uint256 before = _bal(user);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.DisputeResolved(ID1, true, AMT);
        _resolve(ID1, true);
        assertEq(_bal(user), before + AMT);
        assertEq(_st(ID1), uint8(REVERSED));
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function test_After_ArbiterReleasesToPoolBeforeDeadline() public {
        uint256 dl = _lockCompleteRelayedDispute(ID1);
        vm.warp(dl - 1);
        uint256 before = _bal(user);
        vm.expectEmit(true, false, false, true, address(vault));
        emit ScanPayVault.DisputeResolved(ID1, false, AMT);
        _resolve(ID1, false);
        assertEq(_bal(user), before);
        assertEq(_st(ID1), uint8(RELEASED));
        assertEq(vault.poolBalance(), AMT);
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function test_After_ArbiterExpiredAtDeadline() public {
        uint256 dl = _lockCompleteRelayedDispute(ID1);
        vm.warp(dl);
        _expectExpired(dl);
        _resolve(ID1, true);
        _expectExpired(dl);
        _resolve(ID1, false);
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    function test_After_ThirdPartyClaimPaysItemUserAfterDeadline() public {
        uint256 dl = _lockCompleteRelayedDispute(ID1);
        vm.warp(dl - 1);
        _expectTooEarly(dl);
        _claim(rando, ID1);
        vm.warp(dl);
        uint256 userBefore = _bal(user);
        uint256 randoBefore = _bal(rando);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.DisputeTimedOut(ID1, user, AMT);
        _claim(rando, ID1);
        assertEq(_bal(user), userBefore + AMT, "user paid exactly amount");
        assertEq(_bal(rando), randoBefore, "claimer gets nothing");
        assertEq(_bal(relayer), 0, "relayer gets nothing");
        assertEq(_st(ID1), uint8(REVERSED));
        assertEq(vault.heldTotal(), 0);
        assertEq(_bal(address(vault)), 0);
    }

    function test_After_ReleaseReverseRefundReclaimCompleteAllInvalidStatus() public {
        _lockCompleteRelayedDispute(ID1);
        vm.warp(block.timestamp + 2 days); // well past the original dispute window
        _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
        vault.releaseOfframp(ID1);
        _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        _expectInvalidStatus(ID1, DISPUTED, LOCKED);
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        _expectInvalidStatus(ID1, DISPUTED, LOCKED);
        vault.reclaimOfframp(ID1);
        _expectInvalidStatus(ID1, DISPUTED, LOCKED);
        vm.prank(settler);
        vault.completeOfframp(ID1, keccak256("another-ref"));
        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(vault.heldTotal(), AMT);
    }

    function test_After_IndependentIdsKeepIndependentOutcomes() public {
        _lockComplete(ID1, AMT); // relayed dispute
        _lockComplete(ID2, AMT); // untouched -> release
        _lockComplete(ID3, AMT); // direct dispute
        _relayedDispute(ID1);
        _dispute(ID3);
        uint256 rel2 = _dl(ID2).releasableAt;
        vm.warp(rel2);
        vault.releaseOfframp(ID2);
        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(_st(ID2), uint8(RELEASED));
        assertEq(_st(ID3), uint8(DISPUTED));
        assertEq(vault.heldTotal(), 2 * AMT);
        assertEq(vault.poolBalance(), AMT);
        _resolve(ID1, true);
        _resolve(ID3, false);
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 2 * AMT);
        _assertAccounting();
    }

    // ------------------------------------------------------------ (h) dishonest settler
    function test_DishonestSettler_ZeroGasUserDisputesAndIsPaidByTimeout() public {
        vm.deal(user, 0);
        uint256 userStart = _bal(user);
        _lockComplete(ID1, AMT); // settler completes without having paid the INR
        assertEq(_bal(user), userStart - AMT);
        uint64 nonce = vm.getNonce(user);

        // zero-gas user disputes within the window through a relayer
        vm.warp(_dl(ID1).releasableAt - 1);
        _relayedDispute(ID1);
        uint256 dl = _dl(ID1).releasableAt;
        assertEq(user.balance, 0);

        // the settler / owner cannot touch the held funds
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, AMT));
        vm.prank(owner);
        vault.withdrawPool(owner, AMT);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientPoolBalance.selector, 0, AMT));
        vm.prank(settler);
        vault.onramp(keccak256("order"), settler, AMT, 1);
        _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
        vault.releaseOfframp(ID1);
        _expectInvalidStatus(ID1, DISPUTED, COMPLETED);
        vm.prank(settler);
        vault.reverseOfframp(ID1);
        vm.prank(owner);
        vault.sweepSurplus(owner); // no surplus: nothing moves
        assertEq(_bal(owner), 0);
        assertEq(_bal(settler), 0);
        assertEq(_bal(address(vault)), AMT, "funds still custodied");

        // owner tweaking the timeout or arbiter afterwards does not move the user's deadline
        _setTimeout(30 days);
        assertEq(_dl(ID1).releasableAt, dl);

        // the arbiter never answers: wait for the deadline, anyone pays the user back
        vm.warp(dl - 1);
        _expectTooEarly(dl);
        _claim(rando, ID1);
        vm.warp(dl);
        _claim(rando, ID1);
        assertEq(_bal(user), userStart, "user made whole");
        assertEq(_bal(address(vault)), 0);
        assertEq(vault.heldTotal(), 0);
        assertEq(vault.poolBalance(), 0);
        assertEq(_bal(settler), 0);
        assertEq(_bal(owner), 0);
        assertEq(vm.getNonce(user), nonce, "user never sent a tx");
        assertEq(user.balance, 0);
    }

    function test_DishonestSettler_CounterfactualWithoutDisputeFundsReachPool() public {
        _lockComplete(ID1, AMT);
        vm.warp(_dl(ID1).releasableAt);
        vault.releaseOfframp(ID1);
        assertEq(vault.poolBalance(), AMT);
        vm.prank(owner);
        vault.withdrawPool(owner, AMT); // exactly what the relayed dispute prevents
        assertEq(_bal(owner), AMT);
    }

    function test_DishonestSettler_DisputeTooLateFails() public {
        _lockComplete(ID1, AMT);
        uint64 rel = _dl(ID1).releasableAt;
        vm.warp(rel);
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _userSig(ID1, d);
        _expectWindowExpired(rel);
        _relayDispute(ID1, d, sig);
    }
}
