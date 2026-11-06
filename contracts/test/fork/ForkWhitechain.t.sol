// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ForkBase} from "./ForkBase.t.sol";

/// @dev Whitechain Sepolia (chain 1874), USDT with EIP-712 domain ("Tether USD", "1").
/// Runs only when WHITECHAIN_RPC_URL is set.
contract ForkWhitechainTest is ForkBase {
    function _rpcEnv() internal pure override returns (string memory) {
        return "WHITECHAIN_RPC_URL";
    }

    function _chainId() internal pure override returns (uint256) {
        return 1874;
    }

    function _tokenAddr() internal pure override returns (address) {
        return 0xcedfB56113143d55b2358b8CFbB0dEB5Ad2f55a7;
    }

    function _domainName() internal pure override returns (string memory) {
        return "Tether USD";
    }

    function _domainVersion() internal pure override returns (string memory) {
        return "1";
    }

    function _wrongVersion() internal pure override returns (string memory) {
        return "2";
    }

    function _label() internal pure override returns (string memory) {
        return "whitechain";
    }
}
