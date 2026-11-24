from django.db import migrations, models


class Migration(migrations.Migration):
    """SMS-BUS request ids have 21 digits, which overflows bigint. Store provider ids as text."""

    dependencies = [
        ("core", "0006_alter_order_provider"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="provider_order_id",
            field=models.CharField(max_length=64),
        ),
    ]
