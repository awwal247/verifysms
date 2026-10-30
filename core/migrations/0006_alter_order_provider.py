from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0005_order_provider"),
    ]

    operations = [
        migrations.AlterField(
            model_name="order",
            name="provider",
            field=models.CharField(default="smsbus", max_length=20),
        ),
    ]
