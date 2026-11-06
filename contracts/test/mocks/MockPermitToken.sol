// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Nonces} from "@openzeppelin/contracts/utils/Nonces.sol";

/// @title MockPermitToken
/// @notice TEST ONLY: unrestricted minting and configurable EIP-2612 domain/version/decimals.
contract MockPermitToken is ERC20, IERC20Permit, EIP712, Nonces {
    bytes32 public constant PERMIT_TYPEHASH =
        keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)");
    uint8 private immutable _tokenDecimals;

    error PermitExpired(uint256 deadline);
    error InvalidPermitSigner(address signer, address owner);

    /// @notice Configures the name, domain version, and token decimals for tests.
    constructor(string memory name_, string memory version_, uint8 decimals_)
        ERC20(name_, "MOCK") EIP712(name_, version_)
    {
        _tokenDecimals = decimals_;
    }

    /// @notice Returns the test-configured decimals.
    function decimals() public view override returns (uint8) {
        return _tokenDecimals;
    }

    /// @notice Mints test tokens to anyone; intentionally unrestricted.
    function mint(address to, uint256 amount) public {
        _mint(to, amount);
    }

    /// @notice Approves a spender using a standard EIP-2612 permit and consumes the owner's nonce.
    function permit(
        address owner, address spender, uint256 value, uint256 deadline,
        uint8 v, bytes32 r, bytes32 s
    ) public override {
        if (block.timestamp > deadline) revert PermitExpired(deadline);
        bytes32 structHash = keccak256(abi.encode(
            PERMIT_TYPEHASH, owner, spender, value, _useNonce(owner), deadline
        ));
        address signer = ECDSA.recover(_hashTypedDataV4(structHash), v, r, s);
        if (signer != owner) revert InvalidPermitSigner(signer, owner);
        _approve(owner, spender, value);
    }

    /// @notice Returns the next unused EIP-2612 nonce.
    function nonces(address owner) public view override(IERC20Permit, Nonces) returns (uint256) {
        return super.nonces(owner);
    }

    /// @notice Returns the chain-aware, token-specific permit domain separator.
    function DOMAIN_SEPARATOR() external view override returns (bytes32) {
        return _domainSeparatorV4();
    }
}
