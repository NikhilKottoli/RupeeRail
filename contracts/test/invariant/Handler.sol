// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ScanPayVault} from "../../src/ScanPayVault.sol";
import {MockPermitToken} from "../mocks/MockPermitToken.sol";
import {SigHelpers} from "../helpers/SigHelpers.sol";

/// @dev Bounded action surface for invariant fuzzing + ghost accounting of every vault interaction.
contract Handler is SigHelpers {
    uint8 internal constant NONE = 0;
    uint8 internal constant LOCKED = 1;
    uint8 internal constant COMPLETED = 2;
    uint8 internal constant REFUNDED = 3;
    uint8 internal constant REVERSED = 4;

    // action indices for counters
    uint256 internal constant A_OFFRAMP = 0;
    uint256 internal constant A_COMPLETE = 1;
    uint256 internal constant A_REFUND = 2;
    uint256 internal constant A_REVERSE = 3;
    uint256 internal constant A_RECLAIM = 4;
    uint256 internal constant A_ONRAMP = 5;
    uint256 internal constant A_EMIT = 6;
    uint256 internal constant A_FUND = 7;
    uint256 internal constant A_WITHDRAW = 8;
    uint256 internal constant A_SETTERS = 9;
    uint256 internal constant A_PAUSE = 10;
    uint256 internal constant A_WARP = 11;
    uint256 internal constant A_SETTLER = 12;
    uint256 internal constant A_COUNT = 13;

    ScanPayVault public vault;
    MockPermitToken public token;
    address public owner;
    address public relayer = address(0xBEEF01);
    address public funder = address(0xBEEF02);
    address[2] public settlers;
    uint256 public settlerIdx;

    uint256[3] internal actorPks = [uint256(0xA11CE), uint256(0xB0B), uint256(0xCA401)];
    address[3] public actors;
    address[3] public recipients = [address(0xCAFE01), address(0xCAFE02), address(0xCAFE03)];

    bytes32[] public ids;
    uint256 internal nextId;
    mapping(bytes32 => uint8) public lastStatus;
    mapping(bytes32 => address) public idUser;
    mapping(bytes32 => uint256) public idAmount;
    mapping(bytes32 => uint8) public lockedExits; // successful complete + refund + reclaim
    mapping(bytes32 => uint8) public reverseCount;
    mapping(bytes32 => bool) internal onrampSeen;

    uint256 public ghostEscrow;
    uint256 public ghostPool;
    uint256 public ghostIn;
    uint256 public ghostPaidToUsers;
    uint256 public ghostPaidToRecipients;
    mapping(address => uint256) public ghostMinted;
    mapping(address => uint256) public ghostLocked;
    mapping(address => uint256) public ghostReceived;
    bool public badTransition;
    uint256[13] public calls;
    uint256[13] public succ;

    constructor(ScanPayVault v, MockPermitToken t, address owner_, address settler0, address settler1) {
        vault = v;
        token = t;
        owner = owner_;
        settlers = [settler0, settler1];
        for (uint256 i; i < 3; i++) {
            actors[i] = vm.addr(actorPks[i]);
            _mintTo(actors[i], 1_000e6);
        }
    }

    // ------------------------------------------------------------ helpers
    function idCount() external view returns (uint256) {
        return ids.length;
    }

    function settler() public view returns (address) {
        return settlers[settlerIdx];
    }

    function totalSuccess() external view returns (uint256 s) {
        for (uint256 i; i < A_COUNT; i++) s += succ[i];
    }

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
                bytes32 c = ids[(seed + k) % n];
                if (lastStatus[c] == want) return (true, c);
            }
        }
        return (true, ids[seed % n]);
    }

    /// @dev Records a successful transition and checks it is a legal forward move.
    function _apply(bytes32 id, uint8 to) internal {
        uint8 from = lastStatus[id];
        bool legal = (from == NONE && to == LOCKED) || (from == LOCKED && (to == COMPLETED || to == REFUNDED))
            || (from == COMPLETED && to == REVERSED);
        if (!legal || _vaultStatus(id) != to) badTransition = true;
        lastStatus[id] = to;
        uint256 amt = idAmount[id];
        address u = idUser[id];
        if (to == LOCKED) {
            ghostEscrow += amt;
            ghostIn += amt;
            ghostLocked[u] += amt;
        } else if (to == COMPLETED) {
            if (ghostEscrow < amt) badTransition = true;
            else ghostEscrow -= amt;
            ghostPool += amt;
            lockedExits[id]++;
        } else if (to == REFUNDED) {
            if (ghostEscrow < amt) badTransition = true;
            else ghostEscrow -= amt;
            ghostPaidToUsers += amt;
            ghostReceived[u] += amt;
            lockedExits[id]++;
        } else if (to == REVERSED) {
            if (ghostPool < amt) badTransition = true;
            else ghostPool -= amt;
            ghostPaidToUsers += amt;
            ghostReceived[u] += amt;
            reverseCount[id]++;
        }
    }

    // ------------------------------------------------------------ actions
    function offramp(uint256 actorSeed, uint256 amount, uint256 idSeed) public {
        calls[A_OFFRAMP]++;
        _offramp(actorSeed % 3, amount, idSeed);
    }

    function _offramp(uint256 a, uint256 amount, uint256 idSeed) internal returns (bytes32 id) {
        uint256 max = vault.maxOfframpAmount();
        if (max == 0) return bytes32(0);
        amount = bound(amount, 1, max);
        bool reuse = idSeed % 8 == 0 && ids.length > 0;
        id = reuse ? ids[idSeed % ids.length] : keccak256(abi.encode("id", nextId++));
        address u = actors[a];
        if (token.balanceOf(u) < amount) _mintTo(u, 1_000e6);

        ScanPayVault.OfframpIntent memory it = ScanPayVault.OfframpIntent({
            id: id,
            user: u,
            amount: amount,
            inrPaise: 830_000,
            rateE6: 83_000_000,
            qrText: "upi://pay?pa=handler@upi",
            deadline: uint64(block.timestamp + 1 hours)
        });
        bytes memory sig = _signIntent(vault, actorPks[a], it);
        ScanPayVault.PermitSig memory ps =
            _signPermit(address(token), actorPks[a], address(vault), amount, block.timestamp + 1 hours);
        vm.prank(relayer);
        try vault.offramp(it, sig, ps) {
            succ[A_OFFRAMP]++;
            if (lastStatus[id] != NONE) badTransition = true; // id reuse must never succeed
            ids.push(id);
            idUser[id] = u;
            idAmount[id] = amount;
            _apply(id, LOCKED);
        } catch {}
    }

    function complete(uint256 idSeed) public {
        calls[A_COMPLETE]++;
        (bool ok, bytes32 id) = _pick(idSeed, LOCKED);
        if (!ok) return;
        vm.prank(settler());
        try vault.completeOfframp(id, keccak256("ref")) {
            succ[A_COMPLETE]++;
            _apply(id, COMPLETED);
        } catch {}
    }

    /// @dev Compound action that raises the success-path density: lock, wait, complete.
    function lockAndComplete(uint256 actorSeed, uint256 amount, uint256 idSeed) public {
        calls[A_COMPLETE]++;
        bytes32 id = _offramp(actorSeed % 3, amount, idSeed | 1);
        if (id == bytes32(0) || lastStatus[id] != LOCKED) return;
        uint256 d = vault.completeDelay();
        vm.warp(block.timestamp + d);
        vm.prank(settler());
        try vault.completeOfframp(id, keccak256("ref")) {
            succ[A_COMPLETE]++;
            _apply(id, COMPLETED);
        } catch {}
    }

    function refund(uint256 idSeed, uint256 reason) public {
        calls[A_REFUND]++;
        (bool ok, bytes32 id) = _pick(idSeed, LOCKED);
        if (!ok) return;
        uint8 r = uint8(bound(reason, 0, 5)); // 0 and 5 must fail
        vm.prank(settler());
        try vault.refundOfframp(id, r) {
            succ[A_REFUND]++;
            if (r == 0 || r == 5) badTransition = true;
            _apply(id, REFUNDED);
        } catch {}
    }

    function reverse(uint256 idSeed) public {
        calls[A_REVERSE]++;
        (bool ok, bytes32 id) = _pick(idSeed, COMPLETED);
        if (!ok) return;
        vm.prank(settler());
        try vault.reverseOfframp(id) {
            succ[A_REVERSE]++;
            _apply(id, REVERSED);
        } catch {}
    }

    function reclaim(uint256 idSeed) public {
        calls[A_RECLAIM]++;
        (bool ok, bytes32 id) = _pick(idSeed, LOCKED);
        if (!ok) return;
        address caller = idSeed % 8 == 7 ? actors[(idSeed / 8 + 1) % 3] : idUser[id];
        vm.prank(caller);
        try vault.reclaimOfframp(id) {
            succ[A_RECLAIM]++;
            if (caller != idUser[id]) badTransition = true;
            _apply(id, REFUNDED);
        } catch {}
    }

    function onramp(uint256 orderSeed, uint256 rSeed, uint256 amount) public {
        calls[A_ONRAMP]++;
        uint256 pool = vault.poolBalance();
        if (pool == 0) return;
        amount = bound(amount, 1, pool);
        address to = recipients[rSeed % 3];
        bytes32 order = keccak256(abi.encode("order", orderSeed % 24));
        vm.prank(settler());
        try vault.onramp(order, to, amount, 1) {
            succ[A_ONRAMP]++;
            if (onrampSeen[order]) badTransition = true;
            onrampSeen[order] = true;
            _payout(amount);
        } catch {}
    }

    function emitPayment(uint256 rSeed, uint256 amount) public {
        calls[A_EMIT]++;
        uint256 pool = vault.poolBalance();
        if (pool == 0) return;
        amount = bound(amount, 1, pool);
        vm.prank(settler());
        try vault.emitPayment(recipients[rSeed % 3], amount) {
            succ[A_EMIT]++;
            _payout(amount);
        } catch {}
    }

    function _payout(uint256 amount) internal {
        if (ghostPool < amount) badTransition = true;
        else ghostPool -= amount;
        ghostPaidToRecipients += amount;
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
            ghostIn += amount;
        } catch {}
        vm.stopPrank();
    }

    function withdrawPool(uint256 rSeed, uint256 amount) public {
        calls[A_WITHDRAW]++;
        uint256 pool = vault.poolBalance();
        if (pool == 0) return;
        amount = bound(amount, 1, pool);
        vm.prank(owner);
        try vault.withdrawPool(recipients[rSeed % 3], amount) {
            succ[A_WITHDRAW]++;
            _payout(amount);
        } catch {}
    }

    function warp(uint256 dt) public {
        calls[A_WARP]++;
        succ[A_WARP]++;
        vm.warp(block.timestamp + bound(dt, 0, 2 days));
    }

    function setParams(uint256 which, uint256 a, uint256 b) public {
        calls[A_SETTERS]++;
        succ[A_SETTERS]++;
        vm.startPrank(owner);
        uint256 w = which % 4;
        if (w == 0) vault.setCompleteDelay(bound(a, 0, 1 days));
        else if (w == 1) vault.setDisputeWindow(bound(a, 0, 30 days));
        else if (w == 2) vault.setReclaimDelay(bound(a, 1 hours, 30 days));
        else vault.setLimits(bound(a, 1e6, 200e6), bound(b, 0, 2000e6));
        vm.stopPrank();
    }

    /// @dev Pauses ~20% of the time so new-activity paths stay mostly reachable.
    function togglePause(uint256 seed) public {
        calls[A_PAUSE]++;
        succ[A_PAUSE]++;
        vm.prank(owner);
        vault.setPaused(seed % 5 == 0);
    }

    function rotateSettler() public {
        calls[A_SETTLER]++;
        succ[A_SETTLER]++;
        settlerIdx = 1 - settlerIdx;
        vm.prank(owner);
        vault.setSettler(settlers[settlerIdx]);
    }
}
