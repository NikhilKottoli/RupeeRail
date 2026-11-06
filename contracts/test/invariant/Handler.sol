// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../../src/ScanPayVault.sol";
import {MockPermitToken} from "../mocks/MockPermitToken.sol";
import {SigHelpers} from "../helpers/SigHelpers.sol";

/// @dev Bounded action surface for invariant fuzzing + ghost accounting of every vault interaction.
/// Role addresses (owner, settler, arbiter, guardian, relayer) are never token recipients; payouts go to
/// dedicated sink actors so "roles never receive funds" is checkable.
contract Handler is SigHelpers {
    uint8 internal constant NONE = 0;
    uint8 internal constant LOCKED = 1;
    uint8 internal constant COMPLETED = 2;
    uint8 internal constant REFUNDED = 3;
    uint8 internal constant REVERSED = 4;
    uint8 internal constant RELEASED = 5;
    uint8 internal constant DISPUTED = 6;

    // per-id ghost counters
    uint8 internal constant C_COMPLETE = 0;
    uint8 internal constant C_REFUND = 1; // refund + reclaim
    uint8 internal constant C_REVERSE = 2;
    uint8 internal constant C_RELEASE = 3;
    uint8 internal constant C_DISPUTE = 4;
    uint8 internal constant C_RESOLVE = 5;
    uint8 internal constant C_CLAIM = 6; // claimDisputeTimeout

    // action indices for counters
    uint256 internal constant A_OFFRAMP = 0;
    uint256 internal constant A_COMPLETE = 1;
    uint256 internal constant A_REFUND = 2;
    uint256 internal constant A_REVERSE = 3;
    uint256 internal constant A_RECLAIM = 4;
    uint256 internal constant A_DISPUTE = 5;
    uint256 internal constant A_RELEASE = 6;
    uint256 internal constant A_RESOLVE_REFUND = 7;
    uint256 internal constant A_RESOLVE_RELEASE = 8;
    uint256 internal constant A_ONRAMP = 9;
    uint256 internal constant A_FUND = 10;
    uint256 internal constant A_WITHDRAW = 11;
    uint256 internal constant A_DONATE = 12;
    uint256 internal constant A_SWEEP = 13;
    uint256 internal constant A_WARP = 14;
    uint256 internal constant A_SET_COMPLETE_DELAY = 15;
    uint256 internal constant A_SET_DISPUTE_WINDOW = 16;
    uint256 internal constant A_SET_RECLAIM_DELAY = 17;
    uint256 internal constant A_SET_LIMITS = 18;
    uint256 internal constant A_PAUSE = 19;
    uint256 internal constant A_GUARDIAN_PAUSE = 20;
    uint256 internal constant A_CLAIM_TIMEOUT = 21;
    uint256 internal constant A_SET_DISPUTE_TIMEOUT = 22;
    uint256 public constant A_COUNT = 23;

    ScanPayVault public vault;
    MockPermitToken public token;
    address public owner;
    address public settler;
    address public arbiter;
    address public guardian;
    address public constant relayer = address(0xBEEF01);
    address public constant funder = address(0xBEEF02);
    address public constant stranger = address(0xBEEF03);
    uint256 internal qPk;

    uint256[3] internal actorPks = [uint256(0xA11CE), uint256(0xB0B), uint256(0xCA401)];
    address[3] public actors;
    address[2] public onrampSinks = [address(0xCAFE01), address(0xCAFE02)];
    address[2] public withdrawSinks = [address(0xCAFE03), address(0xCAFE04)];
    address[2] public sweepSinks = [address(0xCAFE05), address(0xCAFE06)];

    bytes32[] public ids;
    uint256 internal nextId;
    uint256 internal refCount;
    uint256 internal orderCount;
    mapping(bytes32 => uint8) public lastStatus;
    mapping(bytes32 => address) public idUser;
    mapping(bytes32 => uint256) public idAmount;
    mapping(bytes32 => uint256) public idDelay; // completeDelay snapshotted when the id was locked
    mapping(bytes32 => uint256) public idReclaim; // reclaimDelay snapshot
    mapping(bytes32 => uint256) public idWindow; // disputeWindow snapshot
    mapping(bytes32 => uint256) public idDeadline; // resolve deadline ghost-recorded at dispute time
    mapping(bytes32 => mapping(uint8 => uint8)) public cnt;

    uint256 public ghostEscrow;
    uint256 public ghostHeld;
    uint256 public ghostPool;
    uint256 public ghostSurplus; // directly donated tokens not yet swept
    mapping(address => uint256) public ghostMinted;
    mapping(address => uint256) public ghostLocked;
    mapping(address => uint256) public ghostReceived; // refunds/reversals paid to the ORIGINAL user
    mapping(address => uint256) public ghostSink; // onramp + withdrawPool + sweepSurplus payouts per sink

    /// @dev Highest dailyEmitCap seen while the current emit window was open. The owner may lower or raise the
    /// cap mid-window, so emittedInWindow can legitimately exceed the CURRENT cap but never the highest one.
    uint256 public ghostCapMax;
    uint256 public ghostWindowStart;

    bool public badTransition; // illegal / regressing status move or ghost underflow
    bool public badCompletion; // completion outside [lockedAt + delay, reclaimableAt)
    bool public badSweep; // sweepSurplus changed escrowed/held/pool or paid the wrong amount
    bool public badRule; // anything that must have reverted but succeeded, or a timing rule broken
    uint256[23] public calls;
    uint256[23] public succ;

    struct Pack {
        ScanPayVault.OfframpIntent it;
        bytes sig;
        bytes qsig;
        ScanPayVault.PermitSig ps;
    }

    constructor(ScanPayVault v, MockPermitToken t, address owner_, address settler_, address arbiter_, address guardian_, uint256 quotePk_) {
        vault = v;
        token = t;
        owner = owner_;
        settler = settler_;
        arbiter = arbiter_;
        guardian = guardian_;
        qPk = quotePk_;
        for (uint256 i; i < 3; i++) {
            actors[i] = vm.addr(actorPks[i]);
            _mintTo(actors[i], 1_000e6);
        }
        ghostWindowStart = v.emitWindowStart();
        ghostCapMax = v.dailyEmitCap();
    }

    // ------------------------------------------------------------ views
    function idCount() external view returns (uint256) {
        return ids.length;
    }

    function totalSuccess() external view returns (uint256 s) {
        for (uint256 i; i < A_COUNT; i++) s += succ[i];
    }

    function sinks() external view returns (address[6] memory s) {
        s = [onrampSinks[0], onrampSinks[1], withdrawSinks[0], withdrawSinks[1], sweepSinks[0], sweepSinks[1]];
    }

    // ------------------------------------------------------------ internals
    function _mintTo(address who, uint256 amt) internal {
        token.mint(who, amt);
        ghostMinted[who] += amt;
    }

    function _vaultStatus(bytes32 id) internal view returns (uint8) {
        (,,,,, ScanPayVault.Status st,) = vault.offramps(id);
        return uint8(st);
    }

    function _pick(uint256 seed, uint8 want) internal view returns (bool, bytes32) {
        uint256 n = ids.length;
        if (n == 0) return (false, bytes32(0));
        if (seed % 4 != 0) {
            for (uint256 k; k < n; k++) {
                bytes32 c = ids[((seed % n) + k) % n];
                if (lastStatus[c] == want) return (true, c);
            }
        }
        return (true, ids[seed % n]);
    }

    function _dl(bytes32 id) internal view returns (uint256 c, uint256 r, uint256 w, uint256 rel) {
        (uint64 c_, uint64 r_, uint64 w_, uint64 rel_) = vault.deadlines(id);
        return (c_, r_, w_, rel_);
    }

    function _warpTo(uint256 t) internal {
        if (t > block.timestamp) vm.warp(t);
    }

    function _sub(uint256 a, uint256 b) internal returns (uint256) {
        if (a < b) {
            badTransition = true;
            return 0;
        }
        return a - b;
    }

    function _legal(uint8 f, uint8 t) internal pure returns (bool) {
        if (f == NONE) return t == LOCKED;
        if (f == LOCKED) return t == COMPLETED || t == REFUNDED;
        if (f == COMPLETED) return t == REVERSED || t == RELEASED || t == DISPUTED;
        if (f == DISPUTED) return t == REVERSED || t == RELEASED;
        return false;
    }

    /// @dev Records a successful transition, checks it is a legal forward edge and updates the ghost ledger.
    function _apply(bytes32 id, uint8 from, uint8 to) internal {
        if (lastStatus[id] != from || !_legal(from, to) || _vaultStatus(id) != to) badTransition = true;
        lastStatus[id] = to;
        uint256 amt = idAmount[id];
        address u = idUser[id];
        if (to == LOCKED) {
            ghostEscrow += amt;
            ghostLocked[u] += amt;
        } else if (to == COMPLETED) {
            ghostEscrow = _sub(ghostEscrow, amt);
            ghostHeld += amt;
        } else if (to == REFUNDED) {
            ghostEscrow = _sub(ghostEscrow, amt);
            ghostReceived[u] += amt;
        } else if (to == REVERSED) {
            ghostHeld = _sub(ghostHeld, amt);
            ghostReceived[u] += amt;
        } else if (to == RELEASED) {
            ghostHeld = _sub(ghostHeld, amt);
            ghostPool += amt;
        }
    }

    function _syncWindow() internal {
        uint256 ws = vault.emitWindowStart();
        if (ws != ghostWindowStart) {
            ghostWindowStart = ws;
            ghostCapMax = vault.dailyEmitCap(); // window just rolled inside onramp: cap in force now
        }
    }

    /// @dev Pause only blocks new offramps/onramps; unpause (owner) 3 times out of 4 so the campaign is not
    /// stuck paused for most of a run, while ~25% of attempts still hit the paused path.
    function _maybeUnpause(uint256 seed) internal {
        if (vault.paused() && (seed >> 20) % 4 != 0) {
            vm.prank(owner);
            vault.setPaused(false);
        }
    }

    // ------------------------------------------------------------ offramp
    function offramp(uint256 actorSeed, uint256 amount, uint256 idSeed) public {
        calls[A_OFFRAMP]++;
        _offramp(actorSeed % 3, amount, idSeed, false);
    }

    function _build(uint256 a, bytes32 id, uint256 amount, uint256 mode) internal view returns (Pack memory p) {
        p.it = ScanPayVault.OfframpIntent({
            id: id,
            user: actors[a],
            amount: amount,
            inrPaise: 830_000,
            rateE6: 83_000_000,
            qrCommitment: mode == 11 ? bytes32(0) : _commit(keccak256("handler-salt"), "upi://pay?pa=handler@upi"),
            deadline: uint64(block.timestamp + 1 hours)
        });
        p.sig = _signIntent(vault, actorPks[a], p.it);
        p.qsig = _signQuote(vault, mode == 10 ? 0xBAD : qPk, p.it);
        p.ps = _signPermit(address(token), actorPks[a], address(vault), amount, block.timestamp + 1 hours);
    }

    /// @dev mode 10: quote signed by a foreign key; mode 11: zero qrCommitment (both must revert InvalidQuote).
    function _offramp(uint256 a, uint256 amount, uint256 idSeed, bool good) internal returns (bytes32 id) {
        _maybeUnpause(idSeed);
        uint256 max = vault.maxOfframpAmount();
        if (max == 0) return bytes32(0);
        amount = bound(amount, 1, max);
        uint256 mode = good ? 0 : (idSeed >> 8) % 12;
        bool reuse = !good && idSeed % 8 == 0 && ids.length > 0;
        id = reuse ? ids[(idSeed >> 16) % ids.length] : keccak256(abi.encode("id", nextId++));
        if (token.balanceOf(actors[a]) < amount) _mintTo(actors[a], 1_000e6);
        Pack memory p = _build(a, id, amount, mode);
        if (lastStatus[id] == NONE) {
            idDelay[id] = vault.completeDelay();
            idReclaim[id] = vault.reclaimDelay();
            idWindow[id] = vault.disputeWindow();
        }
        vm.prank(relayer);
        try vault.offramp(p.it, p.sig, p.qsig, p.ps) {
            succ[A_OFFRAMP]++;
            if (mode >= 10 || lastStatus[id] != NONE) badRule = true;
            ids.push(id);
            idUser[id] = actors[a];
            idAmount[id] = amount;
            _apply(id, NONE, LOCKED);
        } catch {}
    }

    // ------------------------------------------------------------ settlement
    function complete(uint256 idSeed, uint256 refSeed) public {
        calls[A_COMPLETE]++;
        (bool ok, bytes32 id) = _pick(idSeed, LOCKED);
        if (!ok) return;
        if (idSeed % 3 == 0) {
            (uint256 c, uint256 r,,) = _dl(id);
            if (c < r) _warpTo(c);
        }
        _tryComplete(id, refSeed);
    }

    /// @dev Compound action that raises success-path density: lock, wait for completableAt, complete.
    function lockAndComplete(uint256 actorSeed, uint256 amount, uint256 idSeed) public {
        calls[A_COMPLETE]++;
        bytes32 id = _offramp(actorSeed % 3, amount, idSeed, true);
        if (id == bytes32(0) || lastStatus[id] != LOCKED) return;
        (uint256 c,,,) = _dl(id);
        _warpTo(c);
        _tryComplete(id, 2);
    }

    function _tryComplete(bytes32 id, uint256 refSeed) internal {
        bytes32 ref;
        bool mustFail;
        if (refSeed % 32 == 1) {
            mustFail = true; // zero payoutRef
        } else if (refSeed % 16 == 0 && refCount > 0) {
            ref = keccak256(abi.encode("ref", (refSeed >> 8) % refCount)); // already-used payoutRef
            mustFail = true;
        } else {
            ref = keccak256(abi.encode("ref", refCount));
        }
        vm.prank(settler);
        try vault.completeOfframp(id, ref) {
            succ[A_COMPLETE]++;
            if (mustFail) badRule = true;
            else refCount++;
            _checkCompletion(id);
            cnt[id][C_COMPLETE]++;
            _apply(id, LOCKED, COMPLETED);
        } catch {}
    }

    /// @dev (h) completion window ghost: lockedAt + snapshotted completeDelay <= completedAt < reclaimableAt.
    function _checkCompletion(bytes32 id) internal {
        (,,, uint64 lockedAt, uint64 completedAt,,) = vault.offramps(id);
        (uint256 ca, uint256 ra, uint256 w, uint256 rel) = _dl(id);
        uint256 lo = uint256(lockedAt) + idDelay[id];
        uint256 hi = uint256(lockedAt) + idReclaim[id];
        if (completedAt != block.timestamp || completedAt < lo || completedAt >= hi) badCompletion = true;
        if (ca != lo || ra != hi || w != idWindow[id] || rel != completedAt + idWindow[id]) badCompletion = true;
    }

    function refund(uint256 idSeed, uint256 reason) public {
        calls[A_REFUND]++;
        (bool ok, bytes32 id) = _pick(idSeed, LOCKED);
        if (!ok) return;
        uint8 r = uint8(bound(reason, 0, 5)); // 0 and 5 must fail
        vm.prank(settler);
        try vault.refundOfframp(id, r) {
            succ[A_REFUND]++;
            if (r == 0 || r == 5) badRule = true;
            cnt[id][C_REFUND]++;
            _apply(id, LOCKED, REFUNDED);
        } catch {}
    }

    function reclaim(uint256 idSeed) public {
        calls[A_RECLAIM]++;
        (bool ok, bytes32 id) = _pick(idSeed, LOCKED);
        if (!ok || (lastStatus[id] != LOCKED && idSeed % 2 == 0)) {
            id = _offramp(idSeed % 3, 1e6 + (idSeed >> 4) % 50e6, idSeed, true); // make something reclaimable
            if (id == bytes32(0) || lastStatus[id] != LOCKED) return;
        }
        if (idSeed % 3 == 0) {
            (, uint256 r,,) = _dl(id);
            _warpTo(r);
        }
        // anyone may reclaim; it must still pay the original user
        address caller = idSeed % 2 == 0 ? idUser[id] : (idSeed % 4 == 1 ? stranger : actors[(idSeed >> 4) % 3]);
        vm.prank(caller);
        try vault.reclaimOfframp(id) {
            succ[A_RECLAIM]++;
            (,,, uint64 lockedAt,,,) = vault.offramps(id);
            if (block.timestamp < uint256(lockedAt) + idReclaim[id]) badRule = true;
            cnt[id][C_REFUND]++;
            _apply(id, LOCKED, REFUNDED);
        } catch {}
    }

    function reverse(uint256 idSeed) public {
        calls[A_REVERSE]++;
        (bool ok, bytes32 id) = _pick(idSeed, COMPLETED);
        if (!ok) return;
        vm.prank(settler);
        try vault.reverseOfframp(id) {
            succ[A_REVERSE]++;
            (,,, uint256 rel) = _dl(id);
            if (block.timestamp >= rel) badRule = true;
            cnt[id][C_REVERSE]++;
            _apply(id, COMPLETED, REVERSED);
        } catch {}
    }

    function dispute(uint256 idSeed) public {
        calls[A_DISPUTE]++;
        (bool ok, bytes32 id) = _pick(idSeed, COMPLETED);
        if (!ok) return;
        address caller = idSeed % 8 == 7 ? stranger : idUser[id];
        (,,, uint256 rel) = _dl(id); // release time (COMPLETED): the dispute window closes here
        uint256 expectedDeadline = block.timestamp + vault.disputeTimeout();
        vm.prank(caller);
        try vault.disputeOfframp(id) {
            succ[A_DISPUTE]++;
            if (caller != idUser[id] || block.timestamp >= rel) badRule = true;
            idDeadline[id] = expectedDeadline; // ghost snapshot: dispute time + disputeTimeout
            (,,, uint256 dl) = _dl(id);
            if (dl != expectedDeadline) badRule = true; // releasableAt must now hold the resolve deadline
            cnt[id][C_DISPUTE]++;
            _apply(id, COMPLETED, DISPUTED);
        } catch {}
    }

    function release(uint256 idSeed) public {
        calls[A_RELEASE]++;
        (bool ok, bytes32 id) = _pick(idSeed, COMPLETED);
        if (!ok) return;
        if (idSeed % 3 == 0) {
            (,,, uint256 rel0) = _dl(id);
            _warpTo(rel0); // releasableAt == 0 for never-completed ids: no-op
        }
        vm.prank(idSeed % 2 == 0 ? stranger : actors[(idSeed >> 4) % 3]);
        try vault.releaseOfframp(id) {
            succ[A_RELEASE]++;
            (,,, uint256 rel) = _dl(id);
            if (block.timestamp < rel) badRule = true;
            cnt[id][C_RELEASE]++;
            _apply(id, COMPLETED, RELEASED);
        } catch {}
    }

    /// @dev Warp mode comes from high seed bits (>> 64) so id picking (low bits) is unaffected. Mode 1 warps to
    /// deadline - 1 (last valid second), mode 2 to the deadline (must be rejected), 0/3 leave the clock alone.
    function resolve(uint256 idSeed, bool refundUser) public {
        uint256 ai = refundUser ? A_RESOLVE_REFUND : A_RESOLVE_RELEASE;
        calls[ai]++;
        (bool ok, bytes32 id) = _pick(idSeed, DISPUTED);
        if (!ok) return;
        if (lastStatus[id] == DISPUTED) {
            uint256 mode = (idSeed >> 64) % 4;
            if (mode == 1) _warpTo(idDeadline[id] - 1);
            else if (mode == 2) _warpTo(idDeadline[id]);
        }
        bool wasDisputed = lastStatus[id] == DISPUTED;
        bool inTime = wasDisputed && block.timestamp < idDeadline[id];
        vm.prank(arbiter);
        try vault.resolveDispute(id, refundUser) {
            succ[ai]++;
            // a resolve may only ever succeed strictly before the snapshotted deadline
            if (!wasDisputed || block.timestamp >= idDeadline[id]) badRule = true;
            cnt[id][C_RESOLVE]++;
            _apply(id, DISPUTED, refundUser ? REVERSED : RELEASED);
        } catch {
            // the arbiter acting in time on a disputed id must never be rejected (pause does not block it)
            if (inTime) badRule = true;
        }
    }

    /// @dev Permissionless timeout claim by a random caller. Mode 0/1 warps to the deadline (must succeed),
    /// mode 2 to deadline - 1 (must be rejected), mode 3 leaves the clock alone.
    function claimTimeout(uint256 idSeed, uint256 callerSeed) public {
        calls[A_CLAIM_TIMEOUT]++;
        (bool ok, bytes32 id) = _pick(idSeed, DISPUTED);
        if (!ok) return;
        if (lastStatus[id] == DISPUTED) {
            uint256 mode = (idSeed >> 64) % 4;
            if (mode <= 1) _warpTo(idDeadline[id]);
            else if (mode == 2) _warpTo(idDeadline[id] - 1);
        }
        address[8] memory pool = [stranger, relayer, settler, owner, arbiter, guardian, actors[0], actors[1]];
        address caller = pool[callerSeed % 8];
        address u = idUser[id];
        uint256 amt = idAmount[id];
        bool wasDisputed = lastStatus[id] == DISPUTED;
        bool due = wasDisputed && block.timestamp >= idDeadline[id];
        uint256 userBefore = token.balanceOf(u);
        uint256 callerBefore = token.balanceOf(caller);
        uint256 vaultBefore = token.balanceOf(address(vault));
        vm.prank(caller);
        try vault.claimDisputeTimeout(id) {
            succ[A_CLAIM_TIMEOUT]++;
            // a claim may only ever succeed at/after the snapshotted deadline
            if (!wasDisputed || block.timestamp < idDeadline[id]) badRule = true;
            // only item.user is paid, exactly amount; the caller (if a different address) gets nothing
            if (token.balanceOf(u) != userBefore + amt || token.balanceOf(address(vault)) != vaultBefore - amt) {
                badRule = true;
            }
            if (caller != u && token.balanceOf(caller) != callerBefore) badRule = true;
            cnt[id][C_CLAIM]++;
            _apply(id, DISPUTED, REVERSED);
        } catch {
            // the mock token never blocks transfers, so a due claim must never be rejected
            if (due) badRule = true;
        }
    }

    /// @dev Owner changes disputeTimeout: ~80% in bounds (must succeed), otherwise just outside (must revert).
    /// Existing disputes keep their snapshot; the invariants compare vault deadlines to idDeadline.
    function setDisputeTimeout(uint256 seed) public {
        calls[A_SET_DISPUTE_TIMEOUT]++;
        bool bad = seed % 5 == 4;
        uint256 v = bound(seed >> 8, 1 days, 30 days);
        if (bad) v = (seed >> 4) % 2 == 0 ? 1 days - 1 - (seed >> 16) % 1 days : 30 days + 1 + (seed >> 16) % 365 days;
        vm.prank(owner);
        try vault.setDisputeTimeout(v) {
            succ[A_SET_DISPUTE_TIMEOUT]++;
            if (bad || vault.disputeTimeout() != v) badRule = true;
        } catch {
            if (!bad) badRule = true;
        }
    }

    // ------------------------------------------------------------ pool / treasury
    function onramp(uint256 orderSeed, uint256 rSeed, uint256 amount) public {
        calls[A_ONRAMP]++;
        _maybeUnpause(orderSeed);
        uint256 top = vault.poolBalance();
        if (top == 0) return;
        if (orderSeed % 4 != 0) {
            uint256 rem = vault.remainingDailyCap();
            if (rem < top) top = rem;
        }
        if (top == 0) return;
        amount = bound(amount, 1, top);
        bool badTo = rSeed % 13 == 0;
        address to = badTo ? ((rSeed >> 8) % 2 == 0 ? address(0) : address(vault)) : onrampSinks[rSeed % 2];
        bool reuse = orderSeed % 6 == 1 && orderCount > 0;
        bytes32 order = keccak256(abi.encode("order", reuse ? (orderSeed >> 8) % orderCount : orderCount));
        vm.prank(settler);
        try vault.onramp(order, to, amount, 1) {
            succ[A_ONRAMP]++;
            if (reuse || badTo) badRule = true;
            if (!reuse) orderCount++;
            ghostPool = _sub(ghostPool, amount);
            ghostSink[to] += amount;
            _syncWindow();
        } catch {}
    }

    function fundPool(uint256 amount) public {
        calls[A_FUND]++;
        amount = bound(amount, 1, 500e6);
        _mintTo(funder, amount);
        vm.startPrank(funder);
        token.approve(address(vault), amount);
        try vault.fundPool(amount) {
            succ[A_FUND]++;
            ghostPool += amount;
        } catch {}
        vm.stopPrank();
    }

    function withdrawPool(uint256 rSeed, uint256 amount) public {
        calls[A_WITHDRAW]++;
        uint256 pool = vault.poolBalance();
        if (pool == 0) return;
        amount = bound(amount, 1, pool);
        address to = withdrawSinks[rSeed % 2];
        vm.prank(owner);
        try vault.withdrawPool(to, amount) {
            succ[A_WITHDRAW]++;
            ghostPool = _sub(ghostPool, amount);
            ghostSink[to] += amount;
        } catch {}
    }

    /// @dev Direct token transfer to the vault: unaccounted surplus, not pool funding.
    function donate(uint256 amount) public {
        calls[A_DONATE]++;
        amount = bound(amount, 1, 200e6);
        token.mint(address(this), amount);
        token.transfer(address(vault), amount);
        succ[A_DONATE]++;
        ghostSurplus += amount;
    }

    function sweep(uint256 rSeed) public {
        calls[A_SWEEP]++;
        uint256 e = vault.escrowedTotal();
        uint256 h = vault.heldTotal();
        uint256 p = vault.poolBalance();
        uint256 bal = token.balanceOf(address(vault));
        address to = sweepSinks[rSeed % 2];
        vm.prank(owner);
        try vault.sweepSurplus(to) {
            if (e != vault.escrowedTotal() || h != vault.heldTotal() || p != vault.poolBalance()) badSweep = true;
            if (bal > e + h + p) {
                succ[A_SWEEP]++;
                if (bal - (e + h + p) != ghostSurplus) badSweep = true;
                ghostSink[to] += bal - (e + h + p);
                ghostSurplus = 0;
            }
        } catch {}
    }

    // ------------------------------------------------------------ time / admin
    function warp(uint256 dt) public {
        calls[A_WARP]++;
        succ[A_WARP]++;
        uint256 k = dt % 4;
        uint256 d = dt >> 8;
        uint256 secs = k == 0 ? bound(d, 0, 60) : k == 1 ? bound(d, 0, 2 hours) : k == 2 ? bound(d, 0, 1 days) : bound(d, 0, 3 days);
        vm.warp(block.timestamp + secs);
    }

    function setParams(uint256 which, uint256 a, uint256 b) public {
        uint256 w = which % 4;
        calls[A_SET_COMPLETE_DELAY + w]++;
        uint256 cd = vault.completeDelay();
        uint256 rd = vault.reclaimDelay();
        vm.startPrank(owner);
        if (w == 0) {
            uint256 hi = rd > 1 hours ? rd - 1 hours : 0; // keep completeDelay + 1h <= reclaimDelay
            if (hi > 1 days) hi = 1 days;
            try vault.setCompleteDelay(bound(a, 0, hi)) {
                succ[A_SET_COMPLETE_DELAY]++;
            } catch {}
        } else if (w == 1) {
            uint256 hi = b % 2 == 0 ? 25 hours : 30 days;
            try vault.setDisputeWindow(bound(a, 1 hours, hi)) {
                succ[A_SET_DISPUTE_WINDOW]++;
            } catch {}
        } else if (w == 2) {
            uint256 lo = cd + 1 hours;
            uint256 hi = b % 2 == 0 ? lo + 48 hours : 30 days;
            if (hi > 30 days) hi = 30 days;
            try vault.setReclaimDelay(bound(a, lo, hi)) {
                succ[A_SET_RECLAIM_DELAY]++;
            } catch {}
        } else {
            uint256 cap = bound(b, 0, 2000e6);
            try vault.setLimits(bound(a, 1e6, 200e6), cap) {
                succ[A_SET_LIMITS]++;
                if (cap > ghostCapMax) ghostCapMax = cap;
            } catch {}
        }
        vm.stopPrank();
    }

    /// @dev Owner toggle: pauses ~20% of the time, otherwise unpauses, so new-activity paths stay reachable.
    function togglePause(uint256 seed) public {
        calls[A_PAUSE]++;
        succ[A_PAUSE]++;
        vm.prank(owner);
        vault.setPaused(seed % 5 == 0);
    }

    function guardianPause(uint256 seed) public {
        calls[A_GUARDIAN_PAUSE]++;
        address caller = seed % 4 == 0 ? stranger : guardian;
        vm.prank(caller);
        try vault.guardianPause() {
            if (caller != guardian) badRule = true;
            else succ[A_GUARDIAN_PAUSE]++;
        } catch {}
    }
}
