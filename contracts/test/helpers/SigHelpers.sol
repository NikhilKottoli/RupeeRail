// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Test} from "forge-std/Test.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {ScanPayVault} from "../../src/ScanPayVault.sol";

/// @dev Shared EIP-712 intent + EIP-2612 permit signing helpers (used by base test and invariant handler).
abstract contract SigHelpers is Test {
    bytes32 internal constant PERMIT_TYPEHASH =
        keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)");

    function _signIntent(ScanPayVault v, uint256 pk, ScanPayVault.OfframpIntent memory it)
        internal
        view
        returns (bytes memory)
    {
        bytes32 d = v.hashIntent(it);
        (uint8 vv, bytes32 r, bytes32 s) = vm.sign(pk, d);
        return abi.encodePacked(r, s, vv);
    }

    /// @dev The quote signer signs the SAME digest as the user (hashIntent).
    function _signQuote(ScanPayVault v, uint256 quotePk, ScanPayVault.OfframpIntent memory it)
        internal
        view
        returns (bytes memory)
    {
        return _signIntent(v, quotePk, it);
    }

    /// @dev Off-chain commitment: keccak256(abi.encode(salt, qrText)).
    function _commit(bytes32 salt, string memory qrText) internal pure returns (bytes32) {
        return keccak256(abi.encode(salt, qrText));
    }

    function _permitDigest(address t, address o, address spender, uint256 value, uint256 deadline)
        internal
        view
        returns (bytes32)
    {
        bytes32 sh = keccak256(abi.encode(PERMIT_TYPEHASH, o, spender, value, IERC20Permit(t).nonces(o), deadline));
        return keccak256(abi.encodePacked("\x19\x01", IERC20Permit(t).DOMAIN_SEPARATOR(), sh));
    }

    function _signPermit(address t, uint256 pk, address spender, uint256 value, uint256 deadline)
        internal
        view
        returns (ScanPayVault.PermitSig memory ps)
    {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(pk, _permitDigest(t, vm.addr(pk), spender, value, deadline));
        ps = ScanPayVault.PermitSig({deadline: deadline, v: v, r: r, s: s});
    }

    function _emptyPermit() internal pure returns (ScanPayVault.PermitSig memory) {
        return ScanPayVault.PermitSig({deadline: 0, v: 0, r: bytes32(0), s: bytes32(0)});
    }
}
