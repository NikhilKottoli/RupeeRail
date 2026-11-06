// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {Script} from "forge-std/Script.sol";
import {console2} from "forge-std/console2.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {ScanPayVault} from "../src/ScanPayVault.sol";

/// @title Deploy
/// @notice Deploy script only; executing it is a separate, explicitly authorized operation.
/// @dev Supply the broadcasting key via the Forge CLI --private-key option, never an env read here.
contract Deploy is Script {
    /// @notice Reads TOKEN/OWNER/SETTLER and optional limits, deploys and returns the vault.
    /// @dev Zero optional limit means retain its default. If the broadcaster is not OWNER,
    /// leaves both defaults unchanged and warns the owner to configure them separately.
    function run() external returns (ScanPayVault vault) {
        address tokenAddress = vm.envAddress("TOKEN");
        address ownerAddress = vm.envAddress("OWNER");
        address settlerAddress = vm.envAddress("SETTLER");
        uint256 maxAmount = vm.envOr("MAX_OFFRAMP_AMOUNT", uint256(0));
        uint256 dailyCap = vm.envOr("DAILY_EMIT_CAP", uint256(0));

        vm.startBroadcast();
        (, address broadcaster,) = vm.readCallers();
        vault = new ScanPayVault(IERC20(tokenAddress), ownerAddress, settlerAddress);
        if (maxAmount != 0 || dailyCap != 0) {
            if (broadcaster == ownerAddress) {
                vault.setLimits(
                    maxAmount == 0 ? vault.maxOfframpAmount() : maxAmount,
                    dailyCap == 0 ? vault.dailyEmitCap() : dailyCap
                );
            } else {
                console2.log("WARNING: broadcaster is not OWNER; owner must call setLimits later.");
                console2.log("Requested MAX_OFFRAMP_AMOUNT (0 means default)", maxAmount);
                console2.log("Requested DAILY_EMIT_CAP (0 means default)", dailyCap);
            }
        }
        vm.stopBroadcast();

        console2.log("ScanPayVault", address(vault));
        console2.log("token", address(vault.token()));
        console2.log("decimals", uint256(vault.decimals()));
        console2.log("owner", vault.owner());
        console2.log("settler", vault.settler());
        console2.log("maxOfframpAmount", vault.maxOfframpAmount());
        console2.log("dailyEmitCap", vault.dailyEmitCap());
        console2.log("completeDelay", vault.completeDelay());
        console2.log("disputeWindow", vault.disputeWindow());
        console2.log("reclaimDelay", vault.reclaimDelay());
        console2.log("chainId", block.chainid);
    }
}
