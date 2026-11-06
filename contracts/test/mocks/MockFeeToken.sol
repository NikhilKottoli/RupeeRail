// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MockPermitToken} from "./MockPermitToken.sol";

/// @title MockFeeToken
/// @notice TEST ONLY: six-decimal permit token burning a configurable transfer fee.
contract MockFeeToken is MockPermitToken {
    uint256 public feeBps = 100;
    error InvalidFeeBps(uint256 value);
    event FeeBpsUpdated(uint256 value);

    /// @notice Sets the permit domain name and version; initial transfer fee is 1%.
    constructor(string memory name_, string memory version_) MockPermitToken(name_, version_, 6) {}

    /// @notice Changes the fee from zero to 10000 bps; intentionally unrestricted for tests.
    function setFeeBps(uint256 value) external {
        if (value > 10_000) revert InvalidFeeBps(value);
        feeBps = value;
        emit FeeBpsUpdated(value);
    }

    function _update(address from, address to, uint256 value) internal override {
        if (from != address(0) && to != address(0)) {
            // Divide first to avoid overflow even for very large test balances.
            uint256 fee = (value / 10_000) * feeBps + ((value % 10_000) * feeBps) / 10_000;
            super._update(from, address(0), fee);
            super._update(from, to, value - fee);
        } else {
            super._update(from, to, value);
        }
    }
}
