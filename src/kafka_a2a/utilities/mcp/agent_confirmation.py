"""
Basic Agent Interaction Tools
============================

These tools provide essential user interaction capabilities for AI agents.
All functions return dictionaries (not JSON strings) for Google SDK compliance.

Usage: Import these functions in your agent code and return the dictionary
directly. The frontend will automatically detect and handle them.
"""

from typing import List, Dict, Any, Optional

def create_confirmation_request(title: str, description: str, action_type: str, details: Optional[str], approve_text: Optional[str], deny_text: Optional[str], allow_input: bool) -> Dict[str, Any]:
    """
    Create a confirmation dialog for user approval.
    
    Perfect for: Deletions, updates, sensitive operations
    
    Args:
        title: Dialog title
        description: What needs confirmation
        action_type: Type of action (delete, update, create, etc.)
        details: Additional context (optional)
        approve_text: Custom approve button text (optional)
        deny_text: Custom deny button text (optional)
        allow_input: Allow user to add extra instructions
    
    Example:
        create_confirmation_request(
            "Delete Products",
            "Are you sure you want to delete 5 products?",
            "delete",
            None,
            None,
            None,
            True
        )
    """
    return {
        "interaction_type": "confirmation_request",
        "title": title,
        "description": description,
        "action_type": action_type,
        "details": details,
        "approve_text": approve_text or "Approve",
        "deny_text": deny_text or "Cancel",
        "allow_input": allow_input
    }

def create_multiple_choice(title: str, description: str, options: List[Dict[str, str]], multiple: bool, allow_input: bool) -> Dict[str, Any]:
    """
    Create a multiple choice selection dialog.
    
    Perfect for: Simple selections, menu choices, option picking
    
    Args:
        title: Dialog title
        description: What user should choose
        options: List of options with 'value' and 'label' keys
        multiple: Allow multiple selections
        allow_input: Allow additional user input
    
    Example:
        create_multiple_choice(
            "Product Action",
            "What would you like to do?",
            [
                {"value": "create", "label": "Create Product"},
                {"value": "update", "label": "Update Product"}
            ],
            False,
            True
        )
    """
    return {
        "interaction_type": "multiple_choice",
        "title": title,
        "description": description,
        "options": options,
        "multiple": multiple,
        "allow_input": allow_input
    }

def create_file_upload_request(title: str, description: str, accepted_types: List[str], max_files: int, max_size_mb: Optional[int]) -> Dict[str, Any]:
    """
    Create a file upload dialog.
    
    Perfect for: Document uploads, image uploads, data imports
    
    Args:
        title: Dialog title
        description: What files are needed
        accepted_types: List of accepted file extensions
        max_files: Maximum number of files
        max_size_mb: Maximum file size in MB (optional)
    
    Example:
        create_file_upload_request(
            "Upload Product Data",
            "Please upload your product catalog",
            ['.csv', '.xlsx'],
            1,
            10
        )
    """
    return {
        "interaction_type": "file_upload",
        "title": title,
        "description": description,
        "accepted_types": accepted_types,
        "max_files": max_files,
        "max_size_mb": max_size_mb or 10
    }

def create_progress_tracker(title: str, total: int, current: int, status: str, eta: Optional[str]) -> Dict[str, Any]:
    """
    Create a progress tracking display.
    
    Perfect for: Long-running operations, batch processing, imports
    
    Args:
        title: Operation title
        total: Total items to process
        current: Current progress
        status: Current status message
        eta: Estimated time remaining (optional)
    
    Example:
        create_progress_tracker(
            "Processing Data",
            100,
            45,
            "Validating entries...",
            "2 minutes"
        )
    """
    return {
        "interaction_type": "progress_tracker",
        "title": title,
        "total": total,
        "current": current,
        "status": status,
        "eta": eta,
        "percentage": round((current / total) * 100, 1) if total > 0 else 0
    }

def create_data_table_review(title: str, description: str, headers: List[str], rows: List[List[str]], editable_columns: Optional[List[int]], allow_add_rows: bool, allow_delete_rows: bool) -> Dict[str, Any]:
    """
    Create a data table for review and editing.
    
    Perfect for: Data validation, bulk editing, table reviews
    
    Args:
        title: Table title
        description: What data is being reviewed
        headers: Column headers
        rows: Table data rows
        editable_columns: Which columns can be edited (optional)
        allow_add_rows: Allow adding new rows
        allow_delete_rows: Allow deleting rows
    
    Example:
        create_data_table_review(
            "Price Changes",
            "Review proposed changes",
            ["Product", "Old Price", "New Price"],
            [["Laptop", "$999", "$899"]],
            [2],
            False,
            False
        )
    """
    return {
        "interaction_type": "data_table_review",
        "title": title,
        "description": description,
        "headers": headers,
        "rows": rows,
        "editable_columns": editable_columns or [],
        "allow_add_rows": allow_add_rows,
        "allow_delete_rows": allow_delete_rows
    }

def create_dynamic_form(title: str, description: str, fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Create a dynamic form with various field types.
    
    Perfect for: Data collection, configuration, user input
    
    Args:
        title: Form title
        description: Form purpose
        fields: List of form fields with type, name, label, required, etc.
    
    Field Types:
        - text: Text input
        - textarea: Multi-line text
        - number: Numeric input
        - select: Dropdown with options
        - boolean: Checkbox
    
    Example:
        create_dynamic_form(
            "Product Details",
            "Enter new product information",
            [
                {"type": "text", "name": "name", "label": "Product Name", "required": True},
                {"type": "number", "name": "price", "label": "Price", "required": True},
                {"type": "boolean", "name": "active", "label": "Is Active", "required": False},
                {"type": "select", "name": "category", "label": "Category", "required": True,
                 "options": [{"value": "electronics", "label": "Electronics"}]}
            ]
        )
    """
    return {
        "interaction_type": "dynamic_form",
        "title": title,
        "description": description,
        "fields": fields
    }

def create_update_form(title: str, description: str, item_id: str, item_name: str, fields: List[Dict[str, Any]], current_values: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a form for updating existing items with pre-populated values.
    
    Perfect for: Editing products, updating records, modifying data
    
    Args:
        title: Form title
        description: Form purpose
        item_id: ID of item being updated
        item_name: Display name of item being updated
        fields: List of form fields (same format as dynamic_form)
        current_values: Current values to pre-populate the form
    
    Example:
        create_update_form(
            "Edit Product",
            "Update product information",
            "prod_123",
            "Laptop Pro",
            [
                {"type": "text", "name": "name", "label": "Product Name", "required": True},
                {"type": "number", "name": "price", "label": "Price", "required": True},
                {"type": "boolean", "name": "active", "label": "Is Active", "required": False}
            ],
            {"name": "Laptop Pro", "price": 1299, "active": True}
        )
    """
    return {
        "interaction_type": "update_form",
        "title": title,
        "description": description,
        "item_id": item_id,
        "item_name": item_name,
        "fields": fields,
        "current_values": current_values
    }

def create_date_time_picker(title: str, description: str, mode: str, min_date: Optional[str], max_date: Optional[str]) -> Dict[str, Any]:
    """
    Create a date/time picker dialog.
    
    Perfect for: Scheduling, date selection, time-based operations
    
    Args:
        title: Picker title
        description: What date/time is needed
        mode: 'date', 'time', or 'datetime'
        min_date: Minimum selectable date (optional)
        max_date: Maximum selectable date (optional)
    
    Example:
        create_date_time_picker(
            "Schedule Task",
            "When should this run?",
            "datetime",
            "2024-01-01",
            None
        )
    """
    return {
        "interaction_type": "date_time_picker",
        "title": title,
        "description": description,
        "type": mode,  # Frontend expects 'type' field
        "min_date": min_date,
        "max_date": max_date
    }

def create_slider_input(title: str, description: str, min_value: int, max_value: int, current_value: int, step: int, unit: str) -> Dict[str, Any]:
    """
    Create a slider input for numeric values.
    
    Perfect for: Percentages, quantities, numeric ranges
    
    Args:
        title: Slider title
        description: What value is being set
        min_value: Minimum value
        max_value: Maximum value
        current_value: Current/default value
        step: Step increment
        unit: Unit label (%, $, etc.)
    
    Example:
        create_slider_input(
            "Set Discount",
            "Choose discount percentage",
            0,
            50,
            10,
            5,
            "%"
        )
    """
    return {
        "interaction_type": "slider_input",
        "title": title,
        "description": description,
        "min": min_value,  # Frontend expects 'min' field
        "max": max_value,  # Frontend expects 'max' field
        "default_value": current_value,  # Frontend expects 'default_value' field
        "step": step,
        "unit": unit
    }

def create_priority_ranking(title: str, description: str, items: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Create a drag-and-drop priority ranking interface.
    
    Perfect for: Prioritization, ordering, ranking tasks
    
    Args:
        title: Ranking title
        description: What needs to be ranked
        items: List of items with 'id' and 'label' keys
    
    Example:
        create_priority_ranking(
            "Feature Priority",
            "Rank features by importance",
            [
                {"id": "auth", "label": "Authentication"},
                {"id": "payment", "label": "Payment System"}
            ]
        )
    """
    return {
        "interaction_type": "priority_ranking",
        "title": title,
        "description": description,
        "items": items
    }

def create_code_review_request(title: str, description: str, changes: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Create a code review interface.
    
    Perfect for: Code changes, file modifications, reviews
    
    Args:
        title: Review title
        description: What changes are being reviewed
        changes: List of changes with file, change_type, old_code, new_code
    
    Example:
        create_code_review_request(
            "Model Changes",
            "Review proposed model updates",
            [{"file": "models.py", "change_type": "modification", "old_code": "...", "new_code": "..."}]
        )
    """
    return {
        "interaction_type": "code_review",
        "title": title,
        "description": description,
        "changes": changes
    }

def create_image_annotation_request(title: str, description: str, image_url: str, annotation_types: List[str]) -> Dict[str, Any]:
    """
    Create an image annotation interface.
    
    Perfect for: Image markup, area selection, visual feedback
    
    Args:
        title: Annotation title
        description: What needs to be annotated
        image_url: URL of image to annotate
        annotation_types: Available annotation tools
    
    Example:
        create_image_annotation_request(
            "Mark Issues",
            "Identify problem areas in the image",
            "https://example.com/image.jpg",
            ["rectangle", "circle", "arrow"]
        )
    """
    return {
        "interaction_type": "image_annotation",
        "title": title,
        "description": description,
        "image_url": image_url,
        "tools": annotation_types  # Frontend expects 'tools' field
    }

if __name__ == "__main__":
    print("Basic Agent Interaction Tools loaded successfully!")
    print("\nExample Multiple Choice:")
    result = create_multiple_choice(
        "Product Action",
        "What would you like to do?",
        [
            {"value": "create", "label": "Create Product"},
            {"value": "update", "label": "Update Product"}
        ],
        False,
        True
    )
    print(result)
