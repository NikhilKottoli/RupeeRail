// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ForkBase} from "./ForkBase.t.sol";
import {ArcNativeCoinEmulator, ArcBlocklistEmulator} from "./ArcNativeCoinEmulator.sol";

/// @dev Arc testnet (chain 5042002), native-gas USDC with EIP-712 domain ("USDC", "2").
/// Runs only when ARC_RPC_URL is set.
contract ForkArcTest is ForkBase {
    address internal constant BLOCKLIST_PRECOMPILE = 0x1800000000000000000000000000000000000001;
    address internal constant NATIVE_COIN_PRECOMPILE = 0x1800000000000000000000000000000000000000;

    /// @dev Arc's USDC ERC-20 is a view over the native balance (18 decimals, scaled 1e12) and moves it via
    /// a node precompile that forge lacks; emulate it so the real USDC code runs end to end.
    function _afterFork() internal override {
        ArcNativeCoinEmulator em = new ArcNativeCoinEmulator();
        vm.allowCheatcodes(NATIVE_COIN_PRECOMPILE);
        vm.etch(NATIVE_COIN_PRECOMPILE, address(em).code);
        vm.etch(BLOCKLIST_PRECOMPILE, address(new ArcBlocklistEmulator()).code);
    }

    /// @dev deal(token) cannot work (no balance slot); balanceOf reads the native balance / 1e12.
    function _fundUser(address to, uint256 amount) internal override {
        vm.deal(to, amount * 1e12);
    }

    function _nativeIsToken() internal pure override returns (bool) {
        return true;
    }

    function _rpcEnv() internal pure override returns (string memory) {
        return "ARC_RPC_URL";
    }

    function _chainId() internal pure override returns (uint256) {
        return 5042002;
    }

    function _tokenAddr() internal pure override returns (address) {
        return 0x3600000000000000000000000000000000000000;
    }

    function _domainName() internal pure override returns (string memory) {
        return "USDC";
    }

    function _domainVersion() internal pure override returns (string memory) {
        return "2";
    }

    function _wrongVersion() internal pure override returns (string memory) {
        return "1";
    }

    function _label() internal pure override returns (string memory) {
        return "arc";
    }
}
