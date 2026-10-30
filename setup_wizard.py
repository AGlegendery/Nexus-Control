#!/usr/bin/env python3
"""
Control Nexus Setup Wizard
Interactive configuration tool for easy deployment
"""

import sys
from pathlib import Path

# Add server directory to path
sys.path.insert(0, str(Path(__file__).parent / "server"))

from config_manager import ConfigManager


def print_banner():
    print("""
╔═══════════════════════════════════════════════════════════╗
║           Control Nexus - Setup Wizard                    ║
║     Easy Configuration for Server and Agents              ║
╚═══════════════════════════════════════════════════════════╝
""")


def get_input(prompt: str, default: str = None) -> str:
    """Get user input with optional default"""
    if default:
        prompt = f"{prompt} [{default}]: "
    else:
        prompt = f"{prompt}: "
    
    value = input(prompt).strip()
    return value if value else default


def confirm(prompt: str, default: bool = True) -> bool:
    """Get yes/no confirmation"""
    default_str = "Y/n" if default else "y/N"
    response = input(f"{prompt} [{default_str}]: ").strip().lower()
    
    if not response:
        return default
    return response in ('y', 'yes')


def setup_server():
    """Setup server configuration"""
    print("\n📋 Server Configuration")
    print("=" * 60)
    
    server_url = get_input("Server URL (for agents to connect)", "http://127.0.0.1:5000")
    admin_username = get_input("Admin username", "admin")
    
    # Password options
    print("\nPassword Options:")
    print("1. Generate secure random password (recommended)")
    print("2. Enter custom password")
    
    choice = get_input("Choose option", "1")
    
    if choice == "2":
        admin_password = get_input("Admin password (min 12 chars)")
        if len(admin_password) < 12:
            print("⚠️  Warning: Password is shorter than recommended 12 characters")
    else:
        admin_password = None  # Will be auto-generated
    
    # Generate configuration
    print("\n🔧 Generating server configuration...")
    
    server_dir = Path(__file__).parent / "server"
    config = ConfigManager.create_env_file(
        base_dir=server_dir,
        server_url=server_url,
        admin_username=admin_username,
        admin_password=admin_password,
    )
    
    print(f"\n✅ Server configuration created: {config['env_file']}")
    print("\n📝 Save these credentials securely:")
    print(f"   Admin Username: {config['admin_username']}")
    print(f"   Admin Password: {config['admin_password']}")
    print(f"   Register Token: {config['register_token']}")
    
    return config


def setup_agent(register_token: str, server_url: str):
    """Setup agent configuration"""
    print("\n📋 Agent Configuration")
    print("=" * 60)
    
    use_defaults = confirm("Use server URL from previous step?", True)
    
    if not use_defaults:
        server_url = get_input("Server URL", server_url)
    
    print(f"\n🔧 Generating agent configuration...")
    print(f"   Server URL: {server_url}")
    print(f"   Register Token: {register_token}")
    
    agent_dir = Path(__file__).parent / "agent"
    env_file = ConfigManager.create_agent_env_file(
        base_dir=agent_dir,
        server_url=server_url,
        register_token=register_token,
    )
    
    print(f"\n✅ Agent configuration created: {env_file}")


def main():
    print_banner()
    
    print("This wizard will help you configure Control Nexus for deployment.")
    print("You can run this wizard multiple times to reconfigure.\n")
    
    if not confirm("Continue with setup?", True):
        print("Setup cancelled.")
        return
    
    # Setup server
    config = setup_server()
    
    # Setup agent
    if confirm("\nConfigure agent now?", True):
        setup_agent(config['register_token'], config['server_url'])
    
    # Next steps
    print("\n" + "=" * 60)
    print("🎉 Setup Complete!")
    print("=" * 60)
    print("\nNext Steps:")
    print("\n1. Start the server:")
    print("   cd server")
    print("   python3 app.py")
    print("\n2. Start the agent:")
    print("   cd agent")
    print("   python3 agent.py")
    print("\n3. Access the dashboard:")
    print("   http://127.0.0.1:5000/login")
    print(f"\n   Username: {config['admin_username']}")
    print(f"   Password: {config['admin_password']}")
    print("\n📚 For more information, see README.md and DEPLOYMENT_GUIDE.md")
    print("\n⚠️  IMPORTANT: Keep your .env files secure and never commit them to git!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        sys.exit(1)
