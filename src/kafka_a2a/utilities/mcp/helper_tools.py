"""
Helper Tools for AI Agents
Internal utility functions for date/time operations, data processing, and business logic.
These tools are for agent internal use only, not for user interaction.
"""

from datetime import datetime, timedelta, date
from typing import Dict, Any, List, Optional, Union
import uuid
import re
import json
from calendar import monthrange
import hashlib



def convert_currency(amount: Union[int, float], from_currency: str, to_currency: str) -> Dict[str, Any]:
    """Convert money value from one currency to another using PyCurrency_Converter"""
    try:
        import PyCurrency_Converter
        
        from_curr = from_currency.upper()
        to_curr = to_currency.upper()
        
        # Same currency conversion
        if from_curr == to_curr:
            return {
                "original_amount": float(amount),
                "from_currency": from_curr,
                "to_currency": to_curr,
                "converted_amount": float(amount),
                "exchange_rate": 1.0,
                "formatted_original": format_currency(amount, from_curr)["formatted"],
                "formatted_converted": format_currency(amount, to_curr)["formatted"],
                "conversion_note": "Same currency - no conversion needed"
            }
        
        # Perform conversion using PyCurrency_Converter
        converted_amount = PyCurrency_Converter.convert(float(amount), from_curr, to_curr)
        
        # Calculate exchange rate
        exchange_rate = converted_amount / float(amount) if float(amount) != 0 else 0
        
        return {
            "original_amount": float(amount),
            "from_currency": from_curr,
            "to_currency": to_curr,
            "converted_amount": round(converted_amount, 4),
            "exchange_rate": round(exchange_rate, 6),
            "formatted_original": format_currency(amount, from_curr)["formatted"],
            "formatted_converted": format_currency(converted_amount, to_curr)["formatted"],
            "conversion_timestamp": datetime.now().isoformat(),
            "note": "Live exchange rates from PyCurrency_Converter"
        }
        
    except ImportError:
        return {"error": "PyCurrency_Converter library not installed. Install with: pip install PyCurrency_Converter"}
    except Exception as e:
        return {"error": f"Currency conversion failed: {str(e)}"}

def get_current_date() -> Dict[str, Any]:
    """Get current date in various formats"""
    now = datetime.now()
    return {
        "date": now.strftime("%Y-%m-%d"),
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "iso_format": now.isoformat(),
        "timestamp": int(now.timestamp()),
        "year": now.year,
        "month": now.month,
        "day": now.day,
        "weekday": now.strftime("%A"),
        "month_name": now.strftime("%B")
    }

def get_date_range(start_date: str, end_date: str) -> Dict[str, Any]:
    """Get date range information and all dates between start and end"""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        
        if start > end:
            return {"error": "Start date must be before end date"}
        
        delta = end - start
        dates = []
        current = start
        
        while current <= end:
            dates.append(current.strftime("%Y-%m-%d"))
            current += timedelta(days=1)
        
        return {
            "start_date": start_date,
            "end_date": end_date,
            "total_days": delta.days + 1,
            "total_weeks": (delta.days + 1) // 7,
            "dates": dates,
            "business_days": get_business_days_count(start_date, end_date)["count"]
        }
    except ValueError as e:
        return {"error": f"Invalid date format. Use YYYY-MM-DD: {str(e)}"}

def get_business_days_count(start_date: str, end_date: str) -> Dict[str, Any]:
    """Count business days (Monday-Friday) between two dates"""
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
        
        business_days = 0
        current = start
        
        while current <= end:
            if current.weekday() < 5:  # Monday = 0, Friday = 4
                business_days += 1
            current += timedelta(days=1)
        
        return {
            "start_date": start_date,
            "end_date": end_date,
            "count": business_days
        }
    except ValueError as e:
        return {"error": f"Invalid date format. Use YYYY-MM-DD: {str(e)}"}

def add_days_to_date(base_date: str, days: int) -> Dict[str, Any]:
    """Add or subtract days from a date"""
    try:
        base = datetime.strptime(base_date, "%Y-%m-%d")
        new_date = base + timedelta(days=days)
        
        return {
            "original_date": base_date,
            "days_added": days,
            "new_date": new_date.strftime("%Y-%m-%d"),
            "new_datetime": new_date.strftime("%Y-%m-%d %H:%M:%S"),
            "weekday": new_date.strftime("%A")
        }
    except ValueError as e:
        return {"error": f"Invalid date format. Use YYYY-MM-DD: {str(e)}"}

def get_month_info(year: int, month: int) -> Dict[str, Any]:
    """Get detailed information about a specific month"""
    try:
        first_day = date(year, month, 1)
        last_day_num = monthrange(year, month)[1]
        last_day = date(year, month, last_day_num)
        
        return {
            "year": year,
            "month": month,
            "month_name": first_day.strftime("%B"),
            "first_day": first_day.strftime("%Y-%m-%d"),
            "last_day": last_day.strftime("%Y-%m-%d"),
            "total_days": last_day_num,
            "first_weekday": first_day.strftime("%A"),
            "last_weekday": last_day.strftime("%A")
        }
    except ValueError as e:
        return {"error": f"Invalid year/month: {str(e)}"}

def get_quarter_info(year: int, quarter: int) -> Dict[str, Any]:
    """Get information about a fiscal quarter"""
    if quarter not in [1, 2, 3, 4]:
        return {"error": "Quarter must be 1, 2, 3, or 4"}
    
    quarter_months = {
        1: [1, 2, 3],
        2: [4, 5, 6], 
        3: [7, 8, 9],
        4: [10, 11, 12]
    }
    
    months = quarter_months[quarter]
    start_date = date(year, months[0], 1)
    end_month_days = monthrange(year, months[2])[1]
    end_date = date(year, months[2], end_month_days)
    
    return {
        "year": year,
        "quarter": quarter,
        "start_date": start_date.strftime("%Y-%m-%d"),
        "end_date": end_date.strftime("%Y-%m-%d"),
        "months": months,
        "total_days": (end_date - start_date).days + 1
    }

def generate_unique_id(prefix: str) -> Dict[str, Any]:
    """Generate unique IDs with optional prefix"""
    unique_id = str(uuid.uuid4())
    short_id = unique_id.split('-')[0]
    
    return {
        "full_id": f"{prefix}_{unique_id}" if prefix else unique_id,
        "short_id": f"{prefix}_{short_id}" if prefix else short_id,
        "uuid": unique_id,
        "timestamp": int(datetime.now().timestamp())
    }

def validate_email(email: str) -> Dict[str, Any]:
    """Validate email address format"""
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    is_valid = bool(re.match(pattern, email))
    
    return {
        "email": email,
        "is_valid": is_valid,
        "domain": email.split('@')[1] if '@' in email else None
    }

def validate_phone(phone: str) -> Dict[str, Any]:
    """Validate phone number format"""
    # Remove all non-digit characters
    digits_only = re.sub(r'\D', '', phone)
    
    # Check various formats
    is_valid = len(digits_only) >= 10
    
    return {
        "original": phone,
        "digits_only": digits_only,
        "is_valid": is_valid,
        "length": len(digits_only)
    }

def format_currency(amount: Union[int, float], currency: str) -> Dict[str, Any]:
    """Format currency amounts"""
    try:
        formatted_amount = f"{float(amount):,.2f}"
        
        currency_symbols = {
            "USD": "$",
            "EUR": "€", 
            "GBP": "£",
            "JPY": "¥"
        }
        
        symbol = currency_symbols.get(currency.upper(), currency.upper())
        
        return {
            "amount": float(amount),
            "currency": currency.upper(),
            "formatted": f"{symbol}{formatted_amount}",
            "symbol": symbol
        }
    except (ValueError, TypeError) as e:
        return {"error": f"Invalid amount: {str(e)}"}

def calculate_percentage(part: Union[int, float], total: Union[int, float]) -> Dict[str, Any]:
    """Calculate percentage with proper formatting"""
    try:
        if total == 0:
            return {"error": "Cannot divide by zero"}
        
        percentage = (float(part) / float(total)) * 100
        
        return {
            "part": float(part),
            "total": float(total),
            "percentage": round(percentage, 2),
            "formatted": f"{percentage:.2f}%"
        }
    except (ValueError, TypeError) as e:
        return {"error": f"Invalid numbers: {str(e)}"}

def generate_sku(category: str, subcategory: str) -> Dict[str, Any]:
    """Generate SKU codes for inventory items"""
    category_code = category[:3].upper()
    subcategory_code = subcategory[:3].upper()
    timestamp = str(int(datetime.now().timestamp()))[-6:]
    
    sku = f"{category_code}-{subcategory_code}-{timestamp}"
    
    return {
        "sku": sku,
        "category": category,
        "subcategory": subcategory,
        "category_code": category_code,
        "subcategory_code": subcategory_code,
        "timestamp": timestamp
    }

def calculate_reorder_point(daily_usage: float, lead_time_days: int, safety_stock: float) -> Dict[str, Any]:
    """Calculate inventory reorder point"""
    try:
        reorder_point = (daily_usage * lead_time_days) + safety_stock
        
        return {
            "daily_usage": daily_usage,
            "lead_time_days": lead_time_days,
            "safety_stock": safety_stock,
            "reorder_point": round(reorder_point, 2),
            "recommendation": "Order now" if reorder_point > 0 else "No order needed"
        }
    except (ValueError, TypeError) as e:
        return {"error": f"Invalid input values: {str(e)}"}

def batch_process_items(items: List[Dict], batch_size: int) -> Dict[str, Any]:
    """Split items into batches for processing"""
    if batch_size <= 0:
        return {"error": "Batch size must be greater than 0"}
    
    batches = []
    for i in range(0, len(items), batch_size):
        batch = items[i:i + batch_size]
        batches.append({
            "batch_number": len(batches) + 1,
            "items": batch,
            "count": len(batch)
        })
    
    return {
        "total_items": len(items),
        "batch_size": batch_size,
        "total_batches": len(batches),
        "batches": batches
    }

def create_audit_entry(action: str, entity_type: str, entity_id: str, user_id: str, details: Optional[Dict]) -> Dict[str, Any]:
    """Create audit trail entry"""
    timestamp = datetime.now()
    
    audit_entry = {
        "audit_id": str(uuid.uuid4()),
        "timestamp": timestamp.isoformat(),
        "action": action,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "user_id": user_id,
        "details": details or {},
        "hash": hashlib.md5(f"{action}{entity_type}{entity_id}{timestamp}".encode()).hexdigest()
    }
    
    return audit_entry

def validate_json_structure(data: str, required_fields: List[str]) -> Dict[str, Any]:
    """Validate JSON data structure"""
    try:
        parsed_data = json.loads(data)
        
        missing_fields = []
        for field in required_fields:
            if field not in parsed_data:
                missing_fields.append(field)
        
        return {
            "is_valid": len(missing_fields) == 0,
            "missing_fields": missing_fields,
            "parsed_data": parsed_data,
            "field_count": len(parsed_data.keys()) if isinstance(parsed_data, dict) else 0
        }
    except json.JSONDecodeError as e:
        return {
            "is_valid": False,
            "error": f"Invalid JSON: {str(e)}",
            "missing_fields": required_fields
        }

def get_working_hours_info(start_hour: int, end_hour: int) -> Dict[str, Any]:
    """Get working hours information"""
    if start_hour < 0 or start_hour > 23 or end_hour < 0 or end_hour > 23:
        return {"error": "Hours must be between 0 and 23"}
    
    if start_hour >= end_hour:
        return {"error": "Start hour must be before end hour"}
    
    total_hours = end_hour - start_hour
    current_hour = datetime.now().hour
    is_working_hours = start_hour <= current_hour < end_hour
    
    return {
        "start_hour": start_hour,
        "end_hour": end_hour,
        "total_hours": total_hours,
        "current_hour": current_hour,
        "is_working_hours": is_working_hours,
        "formatted_range": f"{start_hour:02d}:00 - {end_hour:02d}:00"
    }

if __name__ == "__main__":
    print("Helper Tools loaded successfully!")
    print("\nExample usage:")
    print("Current date:", get_current_date())
    print("Date range:", get_date_range("2024-01-01", "2024-01-07"))
    print("Generated SKU:", generate_sku("Electronics", "Laptops"))
