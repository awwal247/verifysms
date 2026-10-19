from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_broadcast_notification'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='provider',
            field=models.CharField(default='grizzly', max_length=20),
        ),
    ]
