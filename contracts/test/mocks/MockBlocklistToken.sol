// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {MockPermitToken} from "./MockPermitToken.sol";

/// @title MockBlocklistToken
/// @notice TEST ONLY: six-decimal permit token rejecting blocked transfer parties/spenders.
contract MockBlocklistToken is MockPermitToken {
    mapping(address => bool) public blocked;
    error BlockedAddress(address account);
    event BlockedUpdated(address indexed account, bool blocked);

    /// @notice Configures the token's permit domain name and version.
    constructor(string memory name_, string memory version_) MockPermitToken(name_, version_, 6) {}

    /// @notice Changes the test blocklist without access control.
    function setBlocked(address account, bool blocked_) external {
        blocked[account] = blocked_;
        emit BlockedUpdated(account, blocked_);
    }

    /// @notice Transfers only if the spender, sender and recipient are not blocked.
    function transferFrom(address from, address to, uint256 value) public override returns (bool) {
        _checkBlocked(_msgSender());
        return super.transferFrom(from, to, value);
    }

    function _update(address from, address to, uint256 value) internal override {
        // Mint/burn are not transfers; unrestricted mint remains a fixture helper.
        if (from != address(0) && to != address(0)) {
            _checkBlocked(from);
            _checkBlocked(to);
        }
        super._update(from, to, value);
    }

    function _checkBlocked(address account) internal view {
        if (blocked[account]) revert BlockedAddress(account);
    }
}
