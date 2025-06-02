from django.db import models
# Create your models here.

import uuid
from django.utils.timezone import now   

# class User(models.Model):
#     id = models.CharField(primary_key=True, max_length=36, default=uuid.uuid4, editable=False)
#     name = models.CharField(max_length=100)
#     email = models.EmailField(unique=True)
#     phone_number = models.CharField(max_length=15)
#     password = models.CharField(max_length=128)
    
#     created_at = models.DateTimeField(default=now, editable=False)

#     def __str__(self):
#         return self.name
#     class Meta:
#         db_table = 'users'

class User(models.Model):
    ROLE_CHOICES = (
        ('admin', 'Administrator'),
        ('customer', 'Normal User'),
    )
    
    id = models.CharField(primary_key=True, max_length=36, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=100)
    email = models.EmailField(unique=True, null=True, blank=True)  # Cho phép null và blank
    phone_number = models.CharField(max_length=15)
    password = models.CharField(max_length=128)
    role = models.CharField(max_length=10, choices=ROLE_CHOICES, default='customer')
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0.00)  # Số dư tài khoản
    created_at = models.DateTimeField(default=now, editable=False)

    def __str__(self):
        return f"{self.name} ({self.phone_number})"
    
    class Meta:
        db_table = 'users'
    
class Vehicle(models.Model):
    id = models.CharField(primary_key=True, max_length=36, default=uuid.uuid4, editable=False)
    license_plate = models.CharField(max_length=20, unique=True)
    vehicle_type = models.CharField(max_length=50)
    image_path = models.CharField(max_length=255, null=True, blank=True)

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='vehicles')
    created_at = models.DateTimeField(default=now, editable=False)

    def __str__(self):
        return f"{self.license_plate} - {self.vehicle_type}"
    
    class Meta:
        db_table = 'vehicles'

class ParkingSpace(models.Model):
    
    id = models.CharField(primary_key=True,  max_length=36, default=uuid.uuid4, editable=False)
    space_number = models.CharField(max_length=10)
    level = models.IntegerField()
    is_occupied = models.IntegerField(default=0)  # 0 = False, 1 = True
    notes = models.TextField(blank=True, null=True)
    
    def __str__(self):
        return f"Space {self.space_number} - Level {self.level}"
        
    class Meta:
        db_table = 'parkingspace'
        unique_together = ('level', 'space_number')  # Mỗi cặp (level, space_number) phải là duy nhất


class TransactionHistory(models.Model):
    id = models.AutoField(primary_key=True)
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='transactions')
    transaction_type = models.CharField(max_length=20)  # Bỏ choices
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=50, null=True, blank=True)
    status = models.CharField(max_length=20, default='COMPLETED')  # Bỏ choices
    payment = models.ForeignKey('payment.Payment', on_delete=models.SET_NULL, null=True, blank=True, related_name='transactions')
    created_at = models.DateTimeField(default=now, editable=False)

    def __str__(self):
        return f"Giao dịch #{self.id} của {self.user.name} - {self.transaction_type} - {self.amount} VNĐ"

    class Meta:
        db_table = 'transaction_history'
        indexes = [
            models.Index(fields=['payment'])
        ]

