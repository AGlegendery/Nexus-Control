import os
import secrets
from pathlib import Path
from werkzeug.security import generate_password_hash

class ConfigManager:
    """Manages configuration and provides easy setup utilities"""
    
    @staticmethod
    def generate_secret_key(length: int = 32) -> str:
        """Generate a secure random secret key"""
        return secrets.token_urlsafe(length)
    
    @staticmethod
    def generate_register_token(length: int = 32) -> str:
        """Generate a secure registration token"""
        return secrets.token_urlsafe(length)
    
    @staticmethod
    def hash_password(password: str) -> str:
        """Generate password hash"""
        return generate_password_hash(password)
    
    @staticmethod
    def create_env_file(
        base_dir: Path,
        server_url: str = "http://127.0.0.1:5000",
        admin_username: str = "admin",
        admin_password: str = None,
        register_token: str = None,
        flask_secret: str = None,
    ) -> dict:
        """Create .env file with secure defaults"""
        
        # Generate secure values if not provided
        if not register_token:
            register_token = ConfigManager.generate_register_token()
        if not flask_secret:
            flask_secret = ConfigManager.generate_secret_key()
        if not admin_password:
            admin_password = ConfigManager.generate_secret_key(16)
        
        admin_password_hash = ConfigManager.hash_password(admin_password)
        
        env_content = f"""# Control Nexus Server Configuration
# Generated automatically - Keep this file secure!

# Flask Configuration
FLASK_SECRET_KEY={flask_secret}
SERVER_HOST=127.0.0.1
SERVER_PORT=5000
SESSION_COOKIE_SECURE={"1" if server_url.lower().startswith("https://") else "0"}

# Admin Authentication
ADMIN_USERNAME={admin_username}
ADMIN_PASSWORD_HASH={admin_password_hash}

# Agent Registration
REGISTER_TOKEN={register_token}

# Server Settings
OFFLINE_AFTER_SECONDS=35
SCHEDULER_ENABLED=1
SCHEDULER_INTERVAL_SECONDS=15
MAX_CONTENT_LENGTH=20971520
UPLOAD_MAX_MB=20

# Rate Limiting
RATE_LIMIT_ENABLED=1
RATE_LIMIT_PER_MINUTE=300
"""
        
        env_file = base_dir / ".env"
        env_file.write_text(env_content, encoding="utf-8")
        
        return {
            "admin_username": admin_username,
            "admin_password": admin_password,
            "register_token": register_token,
            "flask_secret": flask_secret,
            "server_url": server_url,
            "env_file": str(env_file)
        }
    
    @staticmethod
    def create_agent_env_file(
        base_dir: Path,
        server_url: str,
        register_token: str,
    ) -> str:
        """Create agent .env file"""
        
        env_content = f"""# Control Nexus Agent Configuration

# Server Connection
SERVER_URL={server_url}
REGISTER_TOKEN={register_token}

# Agent Settings
POLL_SECONDS=10
INVENTORY_SYNC_SECONDS=60
REQUEST_TIMEOUT=15
VERIFY_SSL=1
"""
        
        env_file = base_dir / ".env"
        env_file.write_text(env_content, encoding="utf-8")
        
        return str(env_file)
    
    @staticmethod
    def validate_config() -> dict:
        """Validate current configuration"""
        issues = []
        warnings = []
        
        # Check Flask secret
        flask_secret = os.getenv("FLASK_SECRET_KEY", "")
        if flask_secret in {"", "change-this-production-secret", "replace-with-random-hex"}:
            issues.append("FLASK_SECRET_KEY is unset or using the example value - INSECURE!")
        elif len(flask_secret) < 32:
            warnings.append("FLASK_SECRET_KEY is too short (recommended: 32+ chars)")
        
        # Check register token
        register_token = os.getenv("REGISTER_TOKEN", "")
        if register_token in {"", "change-me-register-token", "replace-with-strong-registration-token"}:
            issues.append("REGISTER_TOKEN is unset or using the example value - INSECURE!")
        elif len(register_token) < 16:
            warnings.append("REGISTER_TOKEN is too short (recommended: 16+ chars)")
        
        # Check admin password
        admin_password = os.getenv("ADMIN_PASSWORD", "")
        admin_password_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
        if not admin_password_hash and admin_password in {"", "change-this-now", "replace-with-strong-password"}:
            issues.append("ADMIN_PASSWORD is unset or using the default/example value - INSECURE!")
        elif not admin_password_hash and len(admin_password) < 8:
            warnings.append("ADMIN_PASSWORD is too short (recommended: 12+ chars)")
        
        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "warnings": warnings
        }
