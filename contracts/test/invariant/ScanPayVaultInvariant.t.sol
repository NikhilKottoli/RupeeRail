// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test, console2} from "forge-std/Test.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ScanPayVault} from "../../src/ScanPayVault.sol";
import {MockPermitToken} from "../mocks/MockPermitToken.sol";
import {Handler} from "./Handler.sol";

contract ScanPayVaultInvariantTest is Test {
    ScanPayVault internal vault;
    MockPermitToken internal token;
    Handler internal handler;

    address internal owner = makeAddr("owner");
    address internal settler0 = makeAddr("settler0");
    address internal settler1 = makeAddr("settler1");

    function setUp() public {
        vm.warp(1_700_000_000);
        token = new MockPermitToken("USDC", "2", 6);
        vault = new ScanPayVault(IERC20(address(token)), owner, settler0);
        handler = new Handler(vault, token, owner, settler0, settler1);

        targetContract(address(handler));
        bytes4[] memory sels = new bytes4[](14);
        sels[0] = Handler.offramp.selector;
        sels[1] = Handler.complete.selector;
        sels[2] = Handler.lockAndComplete.selector;
        sels[3] = Handler.refund.selector;
        sels[4] = Handler.reverse.selector;
        sels[5] = Handler.reclaim.selector;
        sels[6] = Handler.onramp.selector;
        sels[7] = Handler.emitPayment.selector;
        sels[8] = Handler.fundPool.selector;
        sels[9] = Handler.withdrawPool.selector;
        sels[10] = Handler.warp.selector;
        sels[11] = Handler.setParams.selector;
        sels[12] = Handler.togglePause.selector;
        sels[13] = Handler.rotateSettler.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: sels}));
    }

    // ---- (a) custody: vault holds at least what it owes, and exactly what the ghost ledger predicts
    function invariant_A_BalanceCoversEscrowPlusPool() public view {
        uint256 bal = token.balanceOf(address(vault));
        assertGe(bal, vault.escrowedTotal() + vault.poolBalance(), "insolvent");
        assertEq(
            bal,
            handler.ghostIn() - handler.ghostPaidToUsers() - handler.ghostPaidToRecipients(),
            "balance != ghost ledger"
        );
        assertEq(vault.poolBalance(), handler.ghostPool(), "pool != ghost pool");
    }

    // ---- (b) escrow equals the sum of LOCKED offramps
    function invariant_B_EscrowEqualsSumOfLocked() public view {
        uint256 sum;
        uint256 n = handler.idCount();
        for (uint256 i; i < n; i++) {
            (, uint96 amt,,,, ScanPayVault.Status st,) = vault.offramps(handler.ids(i));
            if (st == ScanPayVault.Status.LOCKED) sum += amt;
        }
        assertEq(vault.escrowedTotal(), sum, "escrow != sum(LOCKED)");
        assertEq(vault.escrowedTotal(), handler.ghostEscrow(), "escrow != ghost escrow");
    }

    // ---- (c) statuses only move forward along legal edges
    function invariant_C_StatusesMonotonic() public view {
        assertFalse(handler.badTransition(), "illegal transition observed");
        uint256 n = handler.idCount();
        for (uint256 i; i < n; i++) {
            bytes32 id = handler.ids(i);
            (,,,,, ScanPayVault.Status st,) = vault.offramps(id);
            assertEq(uint8(st), handler.lastStatus(id), "vault status != last observed (went backwards?)");
            assertTrue(uint8(st) != 0, "locked id returned to NONE");
        }
    }

    // ---- (d) no double complete/refund/reclaim; reverse at most once and only after complete
    function invariant_D_NoDoubleSettlement() public view {
        uint256 n = handler.idCount();
        for (uint256 i; i < n; i++) {
            bytes32 id = handler.ids(i);
            assertLe(handler.lockedExits(id), 1, "double complete/refund/reclaim");
            assertLe(handler.reverseCount(id), 1, "double reverse");
            if (handler.reverseCount(id) == 1) {
                assertEq(handler.lockedExits(id), 1, "reverse without exit");
                assertEq(handler.lastStatus(id), 4);
            }
        }
    }

    // ---- (e) the settler/relayer/owner never receive escrow; users get back exactly their own refunds
    function invariant_E_FundsOnlyReachRightParties() public view {
        assertEq(token.balanceOf(settler0), 0, "settler0 received tokens");
        assertEq(token.balanceOf(settler1), 0, "settler1 received tokens");
        assertEq(token.balanceOf(handler.relayer()), 0, "relayer received tokens");
        assertEq(token.balanceOf(owner), 0, "owner received tokens");
        uint256 recSum;
        for (uint256 i; i < 3; i++) {
            recSum += token.balanceOf(handler.recipients(i));
            address a = handler.actors(i);
            assertEq(
                token.balanceOf(a),
                handler.ghostMinted(a) - handler.ghostLocked(a) + handler.ghostReceived(a),
                "user balance != minted - locked + own refunds"
            );
        }
        assertEq(recSum, handler.ghostPaidToRecipients(), "recipients got more/less than payouts");
    }

    // ---- accounting identity at the vault level
    function invariant_F_CapNeverExceededInWindow() public view {
        // emittedInWindow may exceed a LOWERED cap (setters shrink it), but never exceeds the largest cap ever set.
        assertLe(vault.emittedInWindow(), 2000e6);
    }

    /// @dev Logs call/success counters per run (visible with -vv).
    function afterInvariant() public view {
        console2.log("offramp calls/succ", handler.calls(0), handler.succ(0));
        console2.log("complete", handler.calls(1), handler.succ(1));
        console2.log("refund", handler.calls(2), handler.succ(2));
        console2.log("reverse", handler.calls(3), handler.succ(3));
        console2.log("reclaim", handler.calls(4), handler.succ(4));
        console2.log("onramp", handler.calls(5), handler.succ(5));
        console2.log("emit", handler.calls(6), handler.succ(6));
        console2.log("fund", handler.calls(7), handler.succ(7));
        console2.log("withdraw", handler.calls(8), handler.succ(8));
    }

    /// @dev Deterministic script proving every handler action has a reachable success path, then re-checks invariants.
    function test_HandlerReachesAllSuccessPaths() public {
        handler.fundPool(100e6);
        handler.offramp(0, 10e6, 1); // id0
        vm.warp(block.timestamp + 15);
        handler.complete(0);
        handler.reverse(0);
        handler.offramp(1, 20e6, 1); // id1
        handler.refund(1, 1);
        handler.offramp(2, 30e6, 1); // id2
        vm.warp(block.timestamp + 24 hours);
        handler.reclaim(2);
        handler.lockAndComplete(0, 5e6, 1);
        handler.onramp(1, 1, 7e6);
        handler.emitPayment(2, 3e6);
        handler.withdrawPool(0, 1e6);

        assertGt(handler.succ(0), 0, "offramp");
        assertGt(handler.succ(1), 0, "complete");
        assertGt(handler.succ(2), 0, "refund");
        assertGt(handler.succ(3), 0, "reverse");
        assertGt(handler.succ(4), 0, "reclaim");
        assertGt(handler.succ(5), 0, "onramp");
        assertGt(handler.succ(6), 0, "emit");
        assertGt(handler.succ(7), 0, "fund");
        assertGt(handler.succ(8), 0, "withdraw");

        invariant_A_BalanceCoversEscrowPlusPool();
        invariant_B_EscrowEqualsSumOfLocked();
        invariant_C_StatusesMonotonic();
        invariant_D_NoDoubleSettlement();
        invariant_E_FundsOnlyReachRightParties();
    }

    /// @dev Handler-driven pause/rotate/setters keep working and the ledger still balances.
    function test_HandlerAdminActionsKeepLedger() public {
        handler.rotateSettler();
        handler.setParams(3, 50e6, 500e6);
        handler.togglePause(0);
        handler.offramp(0, 10e6, 1); // blocked while paused
        assertEq(handler.succ(0), 0);
        handler.togglePause(1);
        handler.offramp(0, 10e6, 1);
        assertEq(handler.succ(0), 1);
        invariant_A_BalanceCoversEscrowPlusPool();
        invariant_E_FundsOnlyReachRightParties();
    }
}
