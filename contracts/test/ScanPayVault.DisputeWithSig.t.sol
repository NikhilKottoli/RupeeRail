// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Vm} from "forge-std/Vm.sol";
import {Pausable} from "@openzeppelin/contracts/utils/Pausable.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";
import {ScanPayVaultDisputeSigBase} from "./ScanPayVault.DisputeWithSig.Base.t.sol";

/// @dev Relayed dispute: zero-gas happy path, parity with the direct path, pause, EIP-712 digest.
contract ScanPayVaultDisputeWithSigTest is ScanPayVaultDisputeSigBase {
    // ------------------------------------------------------------ (a) zero-gas user
    function test_ZeroGasUser_RelayedDispute() public {
        vm.deal(user, 0);
        vm.deal(relayer, 1 ether);
        assertTrue(relayer != user, "relayer must differ from user");
        _lockComplete(ID1, AMT);
        uint256 held = vault.heldTotal();
        uint64 nonce = vm.getNonce(user);
        uint256 userTok = _bal(user);

        // the user only signs; no prank, no tx
        uint64 d = _deadlineIn(1 hours);
        bytes memory sig = _userSig(ID1, d);
        assertEq(vm.getNonce(user), nonce, "signing sends no tx");

        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpDisputed(ID1, user, AMT);
        vm.prank(relayer);
        vault.disputeOfframpWithSig(ID1, d, sig);

        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(_dl(ID1).releasableAt, block.timestamp + vault.disputeTimeout(), "deadline snapshot");
        assertEq(vault.heldTotal(), held, "heldTotal unchanged");
        assertEq(vault.poolBalance(), 0);
        assertEq(_bal(user), userTok, "no token movement for the user");
        assertEq(_bal(relayer), 0, "relayer receives nothing");
        assertEq(user.balance, 0, "user native balance still 0");
        assertEq(vm.getNonce(user), nonce, "user sent no transaction");
        _assertAccounting();
    }

    function test_Relayed_DeadlineEqualToNowIsAccepted() public {
        _lockComplete(ID1, AMT);
        _relayDispute(ID1, uint64(block.timestamp), _userSig(ID1, uint64(block.timestamp)));
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    function test_Relayed_OkOneSecondBeforeReleasable() public {
        _lockComplete(ID1, AMT);
        vm.warp(_dl(ID1).releasableAt - 1);
        _relayedDispute(ID1);
        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(_dl(ID1).releasableAt, block.timestamp + vault.disputeTimeout());
    }

    function test_Relayed_AnyCallerIncludingRolesAndUserMaySubmit() public {
        address[6] memory callers = [relayer, rando, settler, owner, arbiter, user];
        bytes32[6] memory ids = [ID1, ID2, ID3, ID4, ID5, ID6];
        for (uint256 i; i < callers.length; i++) {
            _lockComplete(ids[i], AMT);
            uint64 d = _deadlineIn(1 hours);
            bytes memory sig = _userSig(ids[i], d);
            vm.prank(callers[i]);
            vault.disputeOfframpWithSig(ids[i], d, sig);
            assertEq(_st(ids[i]), uint8(DISPUTED));
        }
        assertEq(vault.heldTotal(), 6 * AMT);
    }

    function test_Relayed_UsesCurrentDisputeTimeoutNotWindow() public {
        _setTimeout(3 days);
        _lockComplete(ID1, AMT);
        _relayedDispute(ID1);
        assertEq(_dl(ID1).releasableAt, block.timestamp + 3 days);
        _setTimeout(20 days); // later change does not move the snapshot
        assertEq(_dl(ID1).releasableAt, block.timestamp + 3 days);
    }

    // ------------------------------------------------------------ (c) direct path unchanged + parity
    function test_Direct_StillWorksForUser() public {
        _lockComplete(ID1, AMT);
        vm.expectEmit(true, true, false, true, address(vault));
        emit ScanPayVault.OfframpDisputed(ID1, user, AMT);
        _dispute(ID1);
        assertEq(_st(ID1), uint8(DISPUTED));
        assertEq(_dl(ID1).releasableAt, block.timestamp + vault.disputeTimeout());
    }

    function test_Direct_StillRevertsUnauthorizedUserForOthers() public {
        _lockComplete(ID1, AMT);
        address[7] memory callers = [settler, owner, arbiter, rando, relayer, guardian, makeAddr("walletSignerLike")];
        for (uint256 i; i < callers.length; i++) {
            vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedUser.selector, callers[i]));
            vm.prank(callers[i]);
            vault.disputeOfframp(ID1);
        }
        // a signature in the relayer's hands does not enable the direct path
        vm.expectRevert(abi.encodeWithSelector(ScanPayVault.UnauthorizedUser.selector, relayer));
        vm.prank(relayer);
        vault.disputeOfframp(ID1);
        assertEq(_st(ID1), uint8(COMPLETED));
    }

    function test_Direct_AtReleasableAtStillExpired() public {
        _lockComplete(ID1, AMT);
        uint64 rel = _dl(ID1).releasableAt;
        vm.warp(rel);
        _expectWindowExpired(rel);
        _dispute(ID1);
    }

    function test_Parity_DirectAndRelayedLeaveIdenticalStateAndLogs() public {
        _lockComplete(ID1, AMT);
        skip(3 hours);
        uint256 snap = vm.snapshotState();

        vm.recordLogs();
        _dispute(ID1);
        Vm.Log[] memory a = vm.getRecordedLogs();
        bytes32 stateA = _stateHash(ID1);

        assertTrue(vm.revertToState(snap));
        assertEq(_st(ID1), uint8(COMPLETED), "snapshot restored");

        vm.recordLogs();
        _relayedDispute(ID1);
        Vm.Log[] memory b = vm.getRecordedLogs();
        bytes32 stateB = _stateHash(ID1);

        assertEq(stateA, stateB, "storage/getters/balances identical");
        assertEq(a.length, 1);
        assertEq(b.length, 1);
        assertEq(a[0].emitter, b[0].emitter);
        assertEq(a[0].data, b[0].data);
        assertEq(a[0].topics.length, b[0].topics.length);
        for (uint256 i; i < a[0].topics.length; i++) {
            assertEq(a[0].topics[i], b[0].topics[i]);
        }
        assertEq(a[0].topics[0], ScanPayVault.OfframpDisputed.selector);
    }

    // ------------------------------------------------------------ (d) paused
    function test_Paused_OwnerPauseStillAllowsRelayedDispute() public {
        _lockComplete(ID1, AMT);
        _pause();
        assertTrue(vault.paused());
        _relayedDispute(ID1);
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    function test_Paused_GuardianPauseStillAllowsRelayedDispute() public {
        _lockComplete(ID1, AMT);
        vm.prank(owner);
        vault.setGuardian(guardian);
        vm.prank(guardian);
        vault.guardianPause();
        assertTrue(vault.paused());

        // pause is effective for new offramps (sanity), but not for the relayed dispute
        ScanPayVault.OfframpIntent memory it = _intent(ID2, user, AMT, QR);
        (bytes memory sig, bytes memory qsig, ScanPayVault.PermitSig memory ps) = _prep(it, userPk, userPk);
        vm.expectRevert(Pausable.EnforcedPause.selector);
        vm.prank(relayer);
        vault.offramp(it, sig, qsig, ps);

        _relayedDispute(ID1);
        assertEq(_st(ID1), uint8(DISPUTED));
    }

    // ------------------------------------------------------------ (g) EIP-712 digest
    function _manualDigest(bytes32 id, address usr, uint64 deadline) internal view returns (bytes32) {
        (, string memory name, string memory version, uint256 chainId, address vc,,) = vault.eip712Domain();
        bytes32 domainSep = keccak256(
            abi.encode(
                keccak256("EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
                keccak256(bytes(name)), keccak256(bytes(version)), chainId, vc
            )
        );
        bytes32 typehash = keccak256("DisputeIntent(bytes32 id,address user,uint64 deadline)");
        bytes32 structHash = keccak256(abi.encode(typehash, id, usr, deadline));
        return keccak256(abi.encodePacked("\x19\x01", domainSep, structHash));
    }

    function test_Hash_TypehashIsExactTypeString() public view {
        assertEq(
            vault.DISPUTE_INTENT_TYPEHASH(), keccak256("DisputeIntent(bytes32 id,address user,uint64 deadline)")
        );
        assertTrue(vault.DISPUTE_INTENT_TYPEHASH() != vault.OFFRAMP_INTENT_TYPEHASH());
    }

    function test_Hash_EqualsManualEip712() public view {
        assertEq(vault.hashDispute(ID1, user, 1_700_003_600), _manualDigest(ID1, user, 1_700_003_600));
        (, string memory name, string memory version, uint256 chainId, address vc,,) = vault.eip712Domain();
        assertEq(name, "ScanPayVault");
        assertEq(version, "1");
        assertEq(chainId, block.chainid);
        assertEq(vc, address(vault));
    }

    function testFuzz_Hash_EqualsManualEip712(bytes32 id, address usr, uint64 deadline) public view {
        assertEq(vault.hashDispute(id, usr, deadline), _manualDigest(id, usr, deadline));
    }

    function test_Hash_BindsEveryField() public view {
        bytes32 base = vault.hashDispute(ID1, user, 100);
        assertTrue(base != vault.hashDispute(ID2, user, 100));
        assertTrue(base != vault.hashDispute(ID1, rando, 100));
        assertTrue(base != vault.hashDispute(ID1, user, 101));
    }
}
