from .models import Setting


def site_context(request):
    """Values shared by the copied frontend templates."""
    return {
        "site_name": Setting.value("site_name", "VerifySMS"),
        "currency_symbol": Setting.value("currency_symbol", "₦"),
        "maintenance_mode": Setting.value("maintenance_mode", "0"),
    }