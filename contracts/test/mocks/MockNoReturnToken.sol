// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {Nonces} from "@openzeppelin/contracts/utils/Nonces.sol";
import {IERC20Permit} from "@openzeppelin/contracts/token/ERC20/extensions/IERC20Permit.sol";

/// @title MockNoReturnToken
/// @notice TEST ONLY: standalone six-decimal token with no return data from ERC-20 writes.
/// @dev Deliberately does not inherit IERC20: approve/transfer/transferFrom return nothing.
contract MockNoReturnToken is EIP712, Nonces, IERC20Permit {
    string public name;
    string public constant symbol = "MOCKNR";
    uint8 public constant decimals = 6;
    uint256 public totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    bytes32 public constant PERMIT_TYPEHASH =
        keccak256("Permit(address owner,address spender,uint256 value,uint256 nonce,uint256 deadline)");

    error ZeroAddress();
    error InsufficientBalance(address account, uint256 available, uint256 required);
    error InsufficientAllowance(address spender, uint256 available, uint256 required);
    error PermitExpired(uint256 deadline);
    error InvalidPermitSigner(address signer, address owner);

    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);

    /// @notice Sets the token name and configurable permit domain version.
    constructor(string memory name_, string memory version_) EIP712(name_, version_) {
        name = name_;
    }

    /// @notice Mints test tokens without access control.
    function mint(address to, uint256 amount) public {
        if (to == address(0)) revert ZeroAddress();
        totalSupply += amount;
        balanceOf[to] += amount;
        emit Transfer(address(0), to, amount);
    }

    /// @notice Transfers tokens and deliberately returns no data.
    function transfer(address to, uint256 amount) external {
        _transfer(msg.sender, to, amount);
    }

    /// @notice Sets allowance and deliberately returns no data.
    function approve(address spender, uint256 amount) external {
        _approve(msg.sender, spender, amount);
    }

    /// @notice Spends allowance, transfers tokens and deliberately returns no data.
    function transferFrom(address from, address to, uint256 amount) external {
        uint256 available = allowance[from][msg.sender];
        if (available != type(uint256).max) {
            if (available < amount) revert InsufficientAllowance(msg.sender, available, amount);
            allowance[from][msg.sender] = available - amount;
        }
        _transfer(from, to, amount);
    }

    /// @notice Sets allowance with an EIP-2612 signature over the configured domain.
    function permit(
        address owner, address spender, uint256 value, uint256 deadline,
        uint8 v, bytes32 r, bytes32 s
    ) external override {
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

    /// @notice Returns the chain-aware permit domain separator.
    function DOMAIN_SEPARATOR() external view override returns (bytes32) {
        return _domainSeparatorV4();
    }

    function _approve(address owner, address spender, uint256 amount) internal {
        if (owner == address(0) || spender == address(0)) revert ZeroAddress();
        allowance[owner][spender] = amount;
        emit Approval(owner, spender, amount);
    }

    function _transfer(address from, address to, uint256 amount) internal {
        if (from == address(0) || to == address(0)) revert ZeroAddress();
        uint256 available = balanceOf[from];
        if (available < amount) revert InsufficientBalance(from, available, amount);
        balanceOf[from] = available - amount;
        balanceOf[to] += amount;
        emit Transfer(from, to, amount);
    }
}
