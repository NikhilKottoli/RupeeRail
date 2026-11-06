// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

/// @title MockERC1271Wallet
/// @notice TEST ONLY: minimal contract wallet. Accepts ERC-1271 signatures made by one configured EOA
/// and lets that EOA make arbitrary calls (approve, dispute, ...) through `execute`.
contract MockERC1271Wallet {
    bytes4 internal constant MAGIC = 0x1626ba7e;
    bytes4 internal constant INVALID = 0xffffffff;

    address public immutable signer;

    error NotSigner(address caller);

    constructor(address signer_) {
        signer = signer_;
    }

    /// @notice ERC-1271: valid iff `signature` is an ECDSA signature of `hash` by `signer`.
    function isValidSignature(bytes32 hash, bytes calldata signature) external view returns (bytes4) {
        (address recovered, ECDSA.RecoverError err,) = ECDSA.tryRecover(hash, signature);
        return (err == ECDSA.RecoverError.NoError && recovered == signer) ? MAGIC : INVALID;
    }

    /// @notice Lets the signer EOA call any target from this wallet; bubbles reverts.
    function execute(address target, bytes calldata data) external returns (bytes memory ret) {
        if (msg.sender != signer) revert NotSigner(msg.sender);
        bool ok;
        (ok, ret) = target.call(data);
        if (!ok) {
            assembly {
                revert(add(ret, 32), mload(ret))
            }
        }
    }
}
