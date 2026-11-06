// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {MockPermitToken} from "./mocks/MockPermitToken.sol";
import {MockNoReturnToken} from "./mocks/MockNoReturnToken.sol";
import {MockERC1271Wallet} from "./mocks/MockERC1271Wallet.sol";
import {ScanPayVaultPermitBase} from "./ScanPayVaultPermitBase.t.sol";

/// @dev ERC-1271 smart-wallet users (allowance path) and gasless relaying per token type.
contract ScanPayVaultPermitWalletTest is ScanPayVaultPermitBase {
    MockERC1271Wallet internal smart;
    address internal walletSigner;
    uint256 internal walletPk;

    function setUp() public override {
        super.setUp();
        (walletSigner, walletPk) = makeAddrAndKey("walletSigner");
        smart = new MockERC1271Wallet(walletSigner);
        _mint(address(smart), 1_000 * UNIT);
    }

    function _walletApprove(uint256 amt) internal {
        vm.prank(walletSigner);
        smart.execute(tokAddr, abi.encodeWithSignature("approve(address,uint256)", address(vault), amt));
    }

    function _walletCall(bytes memory data) internal {
        vm.prank(walletSigner);
        smart.execute(address(vault), data);
    }

    /// @dev Wallet intent; user signature by the wallet's EOA, quote by the quote signer, garbage permit.
    function _walletSigs(ScanPayVault.OfframpIntent memory it, uint256 signerPk)
        internal
        view
        returns (bytes memory, bytes memory)
    {
        return (_signIntent(vault, signerPk, it), _signQuote(vault, quotePk, it));
    }

    function _walletLock(bytes32 id, uint256 amt) internal {
        _walletApprove(amt);
        ScanPayVault.OfframpIntent memory it = _intent(id, address(smart), amt, QR);
        (bytes memory sig, bytes memory qsig) = _walletSigs(it, walletPk);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, _emptyPermit());
    }

    // ------------------------------------------------------------ smart-wallet user
    function test_Wallet_Erc1271UserLocksViaAllowance() public {
        _walletApprove(AMT);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, address(smart), AMT, QR);
        (bytes memory sig, bytes memory qsig) = _walletSigs(it, walletPk);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpLocked(ID1, address(smart), AMT, it.inrPaise, it.rateE6, it.qrCommitment);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, _emptyPermit()); // permit is garbage; allowance carries it
        assertEq(_off(ID1).user, address(smart));
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
        assertEq(_bal(address(smart)), 1_000 * UNIT - AMT);
        assertEq(_bal(address(vault)), AMT);
        assertEq(vault.escrowedTotal(), AMT);
        assertEq(token.allowance(address(smart), address(vault)), 0);
    }

    function test_Wallet_SignatureFromWrongKeyReverts() public {
        _walletApprove(AMT);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, address(smart), AMT, QR);
        (bytes memory sig, bytes memory qsig) = _walletSigs(it, userPk); // not the wallet's signer
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, qsig, _emptyPermit());
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.NONE));
    }

    function test_Wallet_WithoutAllowanceReverts() public {
        ScanPayVault.OfframpIntent memory it = _intent(ID1, address(smart), AMT, QR);
        (bytes memory sig, bytes memory qsig) = _walletSigs(it, walletPk);
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.InsufficientAllowance.selector, 0, AMT));
        vault.offramp(it, sig, qsig, _emptyPermit());
        assertEq(vault.escrowedTotal(), 0);
    }

    function test_Wallet_QuoteSignedByWalletSignerIsNotAQuote() public {
        _walletApprove(AMT);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, address(smart), AMT, QR);
        bytes memory sig = _signIntent(vault, walletPk, it);
        bytes memory fakeQuote = _signIntent(vault, walletPk, it); // wallet signer is not the quote signer
        vm.expectRevert(ScanPayVault.InvalidQuote.selector);
        vault.offramp(it, sig, fakeQuote, _emptyPermit());
    }

    function test_Wallet_SignatureBindsIntentFields() public {
        _walletApprove(AMT);
        ScanPayVault.OfframpIntent memory it = _intent(ID1, address(smart), AMT, QR);
        (bytes memory sig, bytes memory qsig) = _walletSigs(it, walletPk);
        it.amount = AMT - 1; // tamper after signing
        vm.expectRevert(ScanPayVault.InvalidSignature.selector);
        vault.offramp(it, sig, qsig, _emptyPermit());
    }

    function test_Wallet_RefundGoesBackToWallet() public {
        _walletLock(ID1, AMT);
        vm.prank(settler);
        vault.refundOfframp(ID1, 1);
        assertEq(_bal(address(smart)), 1_000 * UNIT);
        assertEq(_bal(address(vault)), 0);
    }

    function test_Wallet_DisputeByWalletAndArbiterRefund() public {
        _walletLock(ID1, AMT);
        skip(15);
        _completeWith(ID1);
        // only the wallet itself (msg.sender == offramp.user) can dispute, not its signer EOA
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedUser.selector, walletSigner));
        vm.prank(walletSigner);
        vault.disputeOfframp(ID1);
        _walletCall(abi.encodeWithSelector(ScanPayVault.disputeOfframp.selector, ID1));
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.DISPUTED));
        vm.prank(arbiter);
        vault.resolveDispute(ID1, true);
        assertEq(_bal(address(smart)), 1_000 * UNIT);
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function test_Wallet_DisputeTimeoutClaimByAnyonePaysWallet() public {
        _walletLock(ID1, AMT);
        skip(15);
        _completeWith(ID1);
        _walletCall(abi.encodeWithSelector(ScanPayVault.disputeOfframp.selector, ID1));
        uint256 dl = _dl(ID1).releasableAt;
        vm.warp(dl);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.DisputeTimedOut(ID1, address(smart), AMT);
        vm.prank(rando);
        vault.claimDisputeTimeout(ID1);
        assertEq(_bal(address(smart)), 1_000 * UNIT, "wallet made whole");
        assertEq(_bal(walletSigner), 0, "signer EOA gets nothing");
        assertEq(_bal(rando), 0);
        assertEq(vault.heldTotal(), 0);
        _assertAccounting();
    }

    function test_Wallet_ReclaimByAnyoneReturnsToWallet() public {
        _walletLock(ID1, AMT);
        skip(24 hours);
        vm.prank(rando);
        vault.reclaimOfframp(ID1);
        assertEq(_bal(address(smart)), 1_000 * UNIT);
    }

    // ------------------------------------------------------------ gasless relay per token type
    /// @dev A user with zero native balance signs permit + intent; a relayer pays all gas.
    function _gaslessLock() internal {
        (address g, uint256 gpk) = makeAddrAndKey("gaslessUser");
        uint256 unit = 10 ** uint256(vault.decimals());
        _mint(g, 1_000 * unit);
        assertEq(g.balance, 0);
        uint64 nonceBefore = vm.getNonce(g);
        uint256 amount = 25 * unit;

        ScanPayVault.OfframpIntent memory it = _intent(ID1, g, amount, QR);
        // sanity: the signed permit is the only thing authorizing the pull (no prior allowance)
        assertEq(IERC20Permit(tokAddr).nonces(g), 0);
        _relay(it, gpk, gpk);

        assertEq(_bal(g), 1_000 * unit - amount, "user balance drops by exactly amount");
        assertEq(_bal(address(vault)), amount);
        assertEq(_off(ID1).user, g);
        assertEq(uint8(_status(ID1)), uint8(ScanPayVault.Status.LOCKED));
        assertEq(vm.getNonce(g), nonceBefore, "user sent no tx");
        assertEq(g.balance, 0, "user paid no gas");
        assertEq(IERC20Permit(tokAddr).nonces(g), 1, "permit consumed");
        assertEq(_bal(relayer), 0);
        _assertAccounting();
    }

    function test_Gasless_Token6Decimals() public {
        assertEq(vault.decimals(), 6);
        _gaslessLock();
    }

    function test_Gasless_Token18Decimals() public {
        MockPermitToken t = new MockPermitToken("Dai-like", "1", 18);
        _deployVault(address(t));
        assertEq(vault.decimals(), 18);
        _gaslessLock();
    }

    function test_Gasless_NoReturnToken() public {
        MockNoReturnToken t = new MockNoReturnToken("Tether USD", "1");
        _deployVault(address(t));
        _gaslessLock();
    }
}
