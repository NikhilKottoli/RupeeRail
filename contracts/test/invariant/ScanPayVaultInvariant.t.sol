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
    address internal settler = makeAddr("settler");
    address internal arbiter = makeAddr("arbiter");
    address internal guardian = makeAddr("guardian");
    address internal quoteSigner;
    uint256 internal quotePk;

    function setUp() public {
        vm.warp(1_700_000_000);
        (quoteSigner, quotePk) = makeAddrAndKey("quoteSigner");
        token = new MockPermitToken("USDC", "2", 6);
        vault = new ScanPayVault(IERC20(address(token)), owner, settler, quoteSigner, arbiter);
        vm.prank(owner);
        vault.setGuardian(guardian);
        handler = new Handler(vault, token, owner, settler, arbiter, guardian, quotePk);

        targetContract(address(handler));
        bytes4[] memory sels = new bytes4[](32);
        sels[0] = Handler.offramp.selector;
        sels[1] = Handler.complete.selector;
        sels[2] = Handler.lockAndComplete.selector;
        sels[3] = Handler.refund.selector;
        sels[4] = Handler.reverse.selector;
        sels[5] = Handler.reclaim.selector;
        sels[6] = Handler.dispute.selector;
        sels[7] = Handler.release.selector;
        sels[8] = Handler.resolve.selector;
        sels[9] = Handler.onramp.selector;
        sels[10] = Handler.fundPool.selector;
        sels[11] = Handler.withdrawPool.selector;
        sels[12] = Handler.donate.selector;
        sels[13] = Handler.sweep.selector;
        sels[14] = Handler.warp.selector;
        sels[15] = Handler.setParams.selector;
        sels[16] = Handler.togglePause.selector;
        sels[17] = Handler.guardianPause.selector;
        // extra weight on the lifecycle actions so deep paths (dispute -> resolve, release, reclaim) are reached
        sels[18] = Handler.lockAndComplete.selector;
        sels[19] = Handler.lockAndComplete.selector;
        sels[20] = Handler.dispute.selector;
        sels[21] = Handler.dispute.selector;
        sels[22] = Handler.resolve.selector;
        sels[23] = Handler.release.selector;
        sels[24] = Handler.release.selector;
        sels[25] = Handler.reclaim.selector;
        sels[26] = Handler.offramp.selector;
        sels[27] = Handler.onramp.selector;
        sels[28] = Handler.claimTimeout.selector;
        sels[29] = Handler.claimTimeout.selector;
        sels[30] = Handler.setDisputeTimeout.selector;
        sels[31] = Handler.resolve.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: sels}));
    }

    // ---- (a) custody: the vault holds at least what it owes; surplus is exactly the donated remainder
    function invariant_A_BalanceCoversAccounting() public view {
        uint256 bal = token.balanceOf(address(vault));
        uint256 accounted = vault.escrowedTotal() + vault.heldTotal() + vault.poolBalance();
        assertGe(bal, accounted, "balance < escrowed + held + pool");
        assertEq(bal - accounted, handler.ghostSurplus(), "surplus != donated-and-unswept");
        assertEq(vault.poolBalance(), handler.ghostPool(), "pool != ghost pool");
    }

    // ---- (b) totals equal the sums over statuses (and the independent ghost ledger)
    function invariant_B_TotalsEqualSumsOfStatuses() public view {
        uint256 locked;
        uint256 held;
        uint256 n = handler.idCount();
        for (uint256 i; i < n; i++) {
            (, uint96 amt,,,, ScanPayVault.Status st,) = vault.offramps(handler.ids(i));
            if (st == ScanPayVault.Status.LOCKED) locked += amt;
            if (st == ScanPayVault.Status.COMPLETED || st == ScanPayVault.Status.DISPUTED) held += amt;
        }
        assertEq(vault.escrowedTotal(), locked, "escrowedTotal != sum(LOCKED)");
        assertEq(vault.heldTotal(), held, "heldTotal != sum(COMPLETED + DISPUTED)");
        assertEq(vault.escrowedTotal(), handler.ghostEscrow(), "escrow != ghost escrow");
        assertEq(vault.heldTotal(), handler.ghostHeld(), "held != ghost held");
    }

    // ---- (c) statuses only move forward along the allowed edges (the handler records every edge)
    function invariant_C_StatusesMonotonic() public view {
        assertFalse(handler.badTransition(), "illegal transition observed");
        uint256 n = handler.idCount();
        for (uint256 i; i < n; i++) {
            bytes32 id = handler.ids(i);
            (,,,,, ScanPayVault.Status st,) = vault.offramps(id);
            assertEq(uint8(st), handler.lastStatus(id), "vault status != last observed (regressed?)");
            assertTrue(uint8(st) != 0, "locked id returned to NONE");
        }
    }

    // ---- (d) no double complete/refund/reverse/release/resolve/claim; complete and refund exclusive;
    // resolve and claim are mutually exclusive and each happens at most once per id
    function invariant_D_NoDoubleSettlement() public view {
        uint256 n = handler.idCount();
        for (uint256 i; i < n; i++) {
            bytes32 id = handler.ids(i);
            uint256 comp = handler.cnt(id, 0);
            uint256 ref = handler.cnt(id, 1);
            uint256 rev = handler.cnt(id, 2);
            uint256 rel = handler.cnt(id, 3);
            uint256 dis = handler.cnt(id, 4);
            uint256 res = handler.cnt(id, 5);
            uint256 clm = handler.cnt(id, 6);
            assertLe(comp, 1, "double complete");
            assertLe(ref, 1, "double refund/reclaim");
            assertLe(comp + ref, 1, "complete and refund both happened");
            assertLe(rev, 1, "double reverse");
            assertLe(rel, 1, "double release");
            assertLe(dis, 1, "double dispute");
            assertLe(res, 1, "double resolve");
            assertLe(clm, 1, "double claim");
            assertLe(res + clm, 1, "resolve and claim both happened");
            assertLe(rev + rel + res + clm, 1, "held funds exited more than once");
            assertLe(dis, comp, "dispute without completion");
            assertLe(res, dis, "resolve without dispute");
            assertLe(clm, dis, "claim without dispute");
            assertLe(rev + rel + res + clm, comp, "held exit without completion");
        }
    }

    // ---- (e) roles never receive funds; users get back exactly their own money; sinks get exactly their payouts
    function invariant_E_FundsOnlyReachRightParties() public view {
        assertEq(token.balanceOf(settler), 0, "settler received tokens");
        assertEq(token.balanceOf(owner), 0, "owner received tokens");
        assertEq(token.balanceOf(arbiter), 0, "arbiter received tokens");
        assertEq(token.balanceOf(guardian), 0, "guardian received tokens");
        assertEq(token.balanceOf(handler.relayer()), 0, "relayer received tokens");
        assertEq(token.balanceOf(handler.stranger()), 0, "stranger received tokens");
        uint256 sum = token.balanceOf(address(vault)) + token.balanceOf(handler.funder());
        for (uint256 i; i < 3; i++) {
            address a = handler.actors(i);
            assertEq(
                token.balanceOf(a),
                handler.ghostMinted(a) - handler.ghostLocked(a) + handler.ghostReceived(a),
                "user balance != minted - locked + own refunds"
            );
            sum += token.balanceOf(a);
        }
        address[6] memory sk = handler.sinks();
        for (uint256 i; i < 6; i++) {
            assertEq(token.balanceOf(sk[i]), handler.ghostSink(sk[i]), "sink balance != chosen payouts");
            sum += token.balanceOf(sk[i]);
        }
        assertEq(token.totalSupply(), sum, "tokens leaked to an unexpected address");
    }

    // ---- (f) the per-window onramp cap, against the HIGHEST cap seen while this window was open.
    // The owner may lower or raise dailyEmitCap mid-window, so emittedInWindow can exceed the current cap
    // (after a decrease) but never the highest cap in force during the window.
    function invariant_F_CapNeverExceededInWindow() public view {
        assertEq(vault.emitWindowStart(), handler.ghostWindowStart(), "window start drifted from ghost");
        assertLe(vault.emittedInWindow(), handler.ghostCapMax(), "emitted > highest cap in window");
        assertLe(handler.ghostCapMax(), 2000e6, "ghost cap max above the largest cap the handler sets");
    }

    // ---- (g) sweepSurplus never changes the three totals and pays exactly the surplus
    function invariant_G_SweepNeverTouchesAccounting() public view {
        assertFalse(handler.badSweep(), "sweepSurplus changed accounting or paid wrong amount");
    }

    // ---- (h) completion window: lockedAt + completeDelay <= completedAt < reclaimableAt (snapshotted at lock)
    function invariant_H_CompletionWindow() public view {
        assertFalse(handler.badCompletion(), "completion outside its window");
        uint256 n = handler.idCount();
        for (uint256 i; i < n; i++) {
            bytes32 id = handler.ids(i);
            if (handler.cnt(id, 0) == 0) continue;
            (,,, uint64 lockedAt, uint64 completedAt,,) = vault.offramps(id);
            assertGe(completedAt, uint256(lockedAt) + handler.idDelay(id), "completed before completableAt");
            assertLt(completedAt, uint256(lockedAt) + handler.idReclaim(id), "completed at/after reclaimableAt");
        }
    }

    // ---- (j) the resolve deadline is snapshotted at dispute time and never moves afterwards, no matter how the
    // owner changes disputeTimeout; releasableAt of a disputed id equals the ghost-recorded value forever.
    function invariant_J_DisputeDeadlineSnapshotIsImmutable() public view {
        uint256 n = handler.idCount();
        for (uint256 i; i < n; i++) {
            bytes32 id = handler.ids(i);
            if (handler.cnt(id, 4) == 0) continue; // never disputed
            (,,, uint64 releasableAt) = vault.deadlines(id);
            assertEq(releasableAt, handler.idDeadline(id), "dispute deadline drifted from its snapshot");
            (,,,,, ScanPayVault.Status st,) = vault.offramps(id);
            if (st == ScanPayVault.Status.DISPUTED) assertEq(handler.cnt(id, 5) + handler.cnt(id, 6), 0);
        }
        assertGe(vault.disputeTimeout(), vault.MIN_DISPUTE_TIMEOUT());
        assertLe(vault.disputeTimeout(), vault.MAX_DISPUTE_TIMEOUT());
    }

    // ---- misc rules: things that must revert did, wrong callers were rejected, timing rules hold
    function invariant_I_NoForbiddenSuccess() public view {
        assertFalse(handler.badRule(), "a forbidden call succeeded or a timing rule broke");
    }

    /// @dev Logs call/success counters per run (visible with -vv).
    function afterInvariant() public view {
        string[23] memory names = [
            "offramp", "complete", "refund", "reverse", "reclaim", "dispute", "release", "resolveRefund",
            "resolveRelease", "onramp", "fund", "withdraw", "donate", "sweep", "warp", "setCompleteDelay",
            "setDisputeWindow", "setReclaimDelay", "setLimits", "pause", "guardianPause", "claimTimeout",
            "setDisputeTimeout"
        ];
        for (uint256 i; i < 23; i++) {
            console2.log(names[i], handler.calls(i), handler.succ(i));
        }
    }

    function _checkAll() internal view {
        invariant_A_BalanceCoversAccounting();
        invariant_B_TotalsEqualSumsOfStatuses();
        invariant_C_StatusesMonotonic();
        invariant_D_NoDoubleSettlement();
        invariant_E_FundsOnlyReachRightParties();
        invariant_F_CapNeverExceededInWindow();
        invariant_G_SweepNeverTouchesAccounting();
        invariant_H_CompletionWindow();
        invariant_J_DisputeDeadlineSnapshotIsImmutable();
        invariant_I_NoForbiddenSuccess();
    }

    /// @dev Deterministic script proving every handler action has a reachable success path, then re-checks invariants.
    function test_HandlerReachesAllSuccessPaths() public {
        handler.fundPool(100e6);
        // id0: lock, complete, reverse
        handler.offramp(0, 10e6, 1);
        vm.warp(block.timestamp + 15);
        handler.complete(1, 2);
        handler.reverse(1);
        // id1: lock, refund
        handler.offramp(1, 20e6, 1);
        handler.refund(1, 1);
        // id2: lock, reclaim after reclaimDelay
        handler.offramp(2, 30e6, 1);
        vm.warp(block.timestamp + 24 hours);
        handler.reclaim(1);
        // id3: lock+complete, dispute, resolve (refund)
        handler.lockAndComplete(0, 5e6, 1);
        handler.dispute(1);
        handler.resolve(1, true);
        // id4: lock+complete, dispute, resolve (release to pool)
        handler.lockAndComplete(1, 6e6, 3);
        handler.dispute(1);
        handler.resolve(1, false);
        // id4b: lock+complete, dispute, arbiter silent -> anyone claims the timeout back to the user
        handler.lockAndComplete(2, 4e6, 9);
        handler.dispute(1);
        handler.claimTimeout(1, 0);
        // id4c: arbiter acts at the last valid second (deadline - 1); claim mode warps are skipped (mode 0 below)
        handler.lockAndComplete(0, 3e6, 11);
        handler.dispute(1);
        handler.resolve(1 | (uint256(1) << 64), true);
        // id5: lock+complete, release after the dispute window
        handler.lockAndComplete(2, 7e6, 5);
        vm.warp(block.timestamp + 24 hours);
        handler.release(1);
        // treasury
        handler.onramp(1, 1, 7e6);
        handler.withdrawPool(0, 1e6);
        handler.donate(9e6);
        handler.sweep(1);
        // admin
        handler.warp(5);
        handler.setParams(0, 20, 0);
        handler.setParams(1, 2 hours, 0);
        handler.setParams(2, 30 hours, 0);
        handler.setParams(3, 50e6, 500e6);
        handler.guardianPause(1);
        handler.togglePause(1); // unpause
        handler.setDisputeTimeout(3 | (uint256(2 days) << 8)); // in-bounds, succeeds

        for (uint256 i; i < 23; i++) {
            assertGt(handler.succ(i), 0, string.concat("no success path for action ", vm.toString(i)));
        }
        _checkAll();
    }

    /// @dev Pause blocks new offramps/onramps; the cap ghost follows a mid-window cap change.
    function test_HandlerPauseAndCapGhost() public {
        handler.togglePause(0); // pause
        handler.offramp(0, 10e6, 1);
        assertEq(handler.succ(0), 0, "offramp succeeded while paused");
        handler.togglePause(1); // unpause
        handler.offramp(0, 10e6, 1);
        assertEq(handler.succ(0), 1);

        handler.fundPool(300e6);
        handler.setParams(3, 50e6, 1500e6); // raise cap mid-window
        assertEq(handler.ghostCapMax(), 1500e6);
        handler.onramp(1, 1, 120e6);
        handler.setParams(3, 50e6, 100e6); // lower below usage
        assertEq(vault.remainingDailyCap(), 0);
        assertEq(handler.ghostCapMax(), 1500e6, "ghost max keeps the highest cap in the window");
        _checkAll();
        vm.warp(block.timestamp + 1 days); // window rolls on next onramp
        handler.onramp(2, 2, 10e6);
        assertEq(handler.ghostCapMax(), 100e6, "ghost resets when the window rolls");
        _checkAll();
    }
}
