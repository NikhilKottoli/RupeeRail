// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Vm} from "forge-std/Vm.sol";

/// @dev TEST-ONLY stand-in for Arc's native-coin precompile at 0x1800...0000. On a real Arc node the
/// USDC ERC-20 (0x3600...) moves balances by calling transfer(address from,address to,uint256 amount18)
/// on that precompile; forge's local EVM has only a 0x01 placeholder there, which underflows. This
/// emulates the precompile by moving native balances with cheatcodes, so the REAL on-chain USDC
/// proxy + implementation bytecode still executes unchanged.
contract ArcNativeCoinEmulator {
    Vm private constant vm = Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    bytes4 private constant TRANSFER = 0xbeabacc8; // transfer(address,address,uint256)

    fallback(bytes calldata data) external returns (bytes memory) {
        require(bytes4(data[:4]) == TRANSFER, "emulator: unsupported selector");
        (address from, address to, uint256 amount) = abi.decode(data[4:], (address, address, uint256));
        require(from.balance >= amount, "emulator: insufficient native balance");
        vm.deal(from, from.balance - amount);
        vm.deal(to, to.balance + amount);
        return abi.encode(true);
    }
}

/// @dev TEST-ONLY stand-in for Arc's blocklist precompile at 0x1800...0001 (isBlocklisted(address)).
/// Nobody is blocklisted; the vault/user/relayer addresses used in these tests are fresh.
contract ArcBlocklistEmulator {
    fallback(bytes calldata data) external returns (bytes memory) {
        require(bytes4(data[:4]) == 0x8e204c43, "emulator: unsupported selector");
        return abi.encode(false);
    }
}
