from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ("product", "0013_product_membership_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="product",
            name="card_replacement_fee",
            field=models.DecimalField(
                db_column="CardReplacementFee",
                max_digits=18,
                decimal_places=2,
                default=1,
                null=False,
                blank=False,
            ),
        ),
    ] 