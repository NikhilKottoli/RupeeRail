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
    /// @notice Reads TOKEN/OWNER/SETTLER/QUOTE_SIGNER/ARBITER and optional guardian/limits, then deploys.
    /// @dev Zero optional limits retain defaults; zero GUARDIAN leaves guardian disabled.
    /// Only an owner broadcaster applies optional settings; otherwise logs owner-action warnings.
    function run() external returns (ScanPayVault vault) {
        address tokenAddress = vm.envAddress("TOKEN");
        address ownerAddress = vm.envAddress("OWNER");
        address settlerAddress = vm.envAddress("SETTLER");
        address quoteSignerAddress = vm.envAddress("QUOTE_SIGNER");
        address arbiterAddress = vm.envAddress("ARBITER");
        address guardianAddress = vm.envOr("GUARDIAN", address(0));
        uint256 maxAmount = vm.envOr("MAX_OFFRAMP_AMOUNT", uint256(0));
        uint256 dailyCap = vm.envOr("DAILY_EMIT_CAP", uint256(0));

        vm.startBroadcast();
        (, address broadcaster,) = vm.readCallers();
        vault = new ScanPayVault(IERC20(tokenAddress), ownerAddress, settlerAddress, quoteSignerAddress, arbiterAddress);
        if (guardianAddress != address(0)) {
            if (broadcaster == ownerAddress) {
                vault.setGuardian(guardianAddress);
            } else {
                console2.log("WARNING: broadcaster is not OWNER; owner must call setGuardian later.");
                console2.log("Requested GUARDIAN", guardianAddress);
            }
        }
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
        console2.log("quoteSigner", vault.quoteSigner());
        console2.log("arbiter", vault.arbiter());
        console2.log("guardian", vault.guardian());
        console2.log("maxOfframpAmount", vault.maxOfframpAmount());
        console2.log("dailyEmitCap", vault.dailyEmitCap());
        console2.log("completeDelay", vault.completeDelay());
        console2.log("disputeWindow", vault.disputeWindow());
        console2.log("disputeTimeout", vault.disputeTimeout());
        console2.log("reclaimDelay", vault.reclaimDelay());
        console2.log("emitWindowStart", vault.emitWindowStart());
        console2.log("emittedInWindow", vault.emittedInWindow());
        console2.log("paused", vault.paused());
        console2.log("chainId", block.chainid);
    }
}
