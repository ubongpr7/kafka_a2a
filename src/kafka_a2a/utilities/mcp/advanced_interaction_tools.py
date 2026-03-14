"""
Advanced Agent Interaction Tools for Dynamic Data
=================================================

These tools handle complex scenarios like large datasets, searchable lists,
hierarchical selections, and other advanced user interactions.

Usage: Import these functions in your agent code and return the JSON string
as your response. The frontend will automatically detect and handle them.
"""

import json
from typing import List, Dict, Any, Optional

def create_searchable_selection(
    title: str,
    description: str,
    items: List[Dict[str, Any]],
    search_fields: List[str],
    multiple: bool,
    show_images: bool,
    allow_additional_input: bool,
    max_selections: Optional[int]
) -> Dict[str, Any]:
    """
    Create a searchable selection interface for large datasets.
    
    Perfect for: Product catalogs, user lists, large inventories
    
    Args:
        title: Dialog title
        description: What user should select
        items: List of items with 'id', 'name', 'description', optional 'image', 'price', 'category'
        search_fields: Which fields to search in
        multiple: Allow multiple selections
        show_images: Display item images if available, Should be True if it exist
        allow_additional_input: Allow user to add instructions
        max_selections: Limit number of selections
    
    Example:
        create_searchable_selection(
            "Select Products",
            "Choose products to add to inventory",
            [
                {"id": "1", "name": "iPhone 15", "description": "Latest smartphone", "price": "$999", "category": "Electronics"},
                {"id": "2", "name": "MacBook Pro", "description": "Professional laptop", "price": "$2499", "category": "Computers"}
            ],
            ["name", "description"],
            True,
            True,
            True,
            None
        )
    """
    return {
        "interaction_type": "searchable_selection",
        "title": title,
        "description": description,
        "items": items,
        "search_fields": search_fields or ["name", "description"],
        "multiple": multiple,
        "show_images": show_images,
        "allow_additional_input": allow_additional_input,
        "max_selections": max_selections
    }


def create_hierarchical_selection(
    title: str,
    description: str,
    tree_data: Dict[str, Any],
    multiple: bool,
    expand_all: bool,
    show_counts: bool
) -> Dict[str, Any]:
    """
    Create a tree-like hierarchical selection interface.
    
    Perfect for: Category selection, organizational charts, nested data
    
    Args:
        title: Dialog title
        description: What user should select
        tree_data: Nested structure with 'id', 'name', 'children' (optional)
        multiple: Allow multiple selections
        expand_all: Start with all nodes expanded
        show_counts: Show item counts in each category
    
    Example:
        create_hierarchical_selection(
            "Select Categories",
            "Choose product categories to update",
            {
                "id": "root",
                "name": "All Categories",
                "children": [
                    {
                        "id": "electronics",
                        "name": "Electronics",
                        "count": 150,
                        "children": [
                            {"id": "phones", "name": "Smartphones", "count": 45},
                            {"id": "laptops", "name": "Laptops", "count": 32}
                        ]
                    }
                ]
            },
            False,
            False,
            True
        )
    """
    return {
        "interaction_type": "hierarchical_selection",
        "title": title,
        "description": description,
        "tree_data": tree_data,
        "multiple": multiple,
        "expand_all": expand_all,
        "show_counts": show_counts
    }

def create_autocomplete_selection(
    title: str,
    description: str,
    placeholder: str,
    min_chars: int,
    api_endpoint: Optional[str],
    static_options: Optional[List[Dict[str, Any]]],
    multiple: bool,
    create_new: bool
) -> Dict[str, Any]:
    """
    Create an autocomplete/type-ahead selection interface.
    
    Perfect for: Large user databases, tag selection, dynamic search
    
    Args:
        title: Dialog title
        description: What user should search for
        placeholder: Input placeholder text
        min_chars: Minimum characters before search starts
        api_endpoint: URL to fetch suggestions (if dynamic)
        static_options: Pre-defined options (if static)
        multiple: Allow multiple selections
        create_new: Allow creating new items
    
    Example:
        create_autocomplete_selection(
            "Select Users",
            "Search and select users to notify",
            "Type username or email...",
            2,
            None,
            [
                {"id": "1", "name": "John Doe", "email": "john@example.com"},
                {"id": "2", "name": "Jane Smith", "email": "jane@example.com"}
            ],
            True,
            False
        )
    """
    return {
        "interaction_type": "autocomplete_selection",
        "title": title,
        "description": description,
        "placeholder": placeholder or "Start typing to search...",
        "min_chars": min_chars,
        "api_endpoint": api_endpoint,
        "static_options": static_options or [],
        "multiple": multiple,
        "create_new": create_new
    }

def create_comparison_view(
    title: str,
    description: str,
    items: List[Dict[str, Any]],
    comparison_fields: List[str],
    allow_selection: bool,
    highlight_differences: bool
) -> Dict[str, Any]:
    """
    Create a side-by-side comparison interface.
    
    Perfect for: Product comparisons, plan selection, feature analysis
    
    Args:
        title: Dialog title
        description: What user should compare
        items: Items to compare with fields matching comparison_fields
        comparison_fields: Which fields to show in comparison
        allow_selection: Allow user to select preferred item
        highlight_differences: Highlight different values
    
    Example:
        create_comparison_view(
            "Compare Plans",
            "Choose the best pricing plan",
            [
                {"id": "basic", "name": "Basic", "price": "$9/month", "users": "5", "storage": "10GB"},
                {"id": "pro", "name": "Pro", "price": "$29/month", "users": "25", "storage": "100GB"}
            ],
            ["price", "users", "storage"],
            True,
            True
        )
    """
    return {
        "interaction_type": "comparison_view",
        "title": title,
        "description": description,
        "items": items,
        "comparison_fields": comparison_fields,
        "allow_selection": allow_selection,
        "highlight_differences": highlight_differences
    }

def create_bulk_action_selector(
    title: str,
    description: str,
    items: List[Dict[str, Any]],
    actions: List[Dict[str, str]],
    filters: Optional[List[Dict[str, Any]]],
    preview_changes: bool
) -> Dict[str, Any]:
    """
    Create a bulk action interface with filtering and preview.
    
    Perfect for: Bulk updates, mass operations, batch processing
    
    Args:
        title: Dialog title
        description: What bulk action to perform
        items: Items available for bulk action
        actions: Available actions with 'id', 'name', 'description'
        filters: Available filters to narrow selection
        preview_changes: Show preview before confirming
    
    Example:
        create_bulk_action_selector(
            "Bulk Update Products",
            "Select products and action to perform",
            [
                {"id": "1", "name": "Product A", "category": "Electronics", "status": "active"},
                {"id": "2", "name": "Product B", "category": "Clothing", "status": "inactive"}
            ],
            [
                {"id": "activate", "name": "Activate", "description": "Make products active"},
                {"id": "discount", "name": "Apply Discount", "description": "Add discount to selected items"}
            ],
            [
                {"field": "category", "type": "select", "options": ["Electronics", "Clothing"]},
                {"field": "status", "type": "select", "options": ["active", "inactive"]}
            ],
            True
        )
    """
    return {
        "interaction_type": "bulk_action_selector",
        "title": title,
        "description": description,
        "items": items,
        "actions": actions,
        "filters": filters or [],
        "preview_changes": preview_changes
    }

def create_conditional_form(
    title: str,
    description: str,
    fields: List[Dict[str, Any]],
    conditions: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Create a form with conditional logic (show/hide fields based on selections).
    
    Perfect for: Complex configurations, dynamic forms, wizard-like interfaces
    
    Args:
        title: Dialog title
        description: Form purpose
        fields: Form fields with conditional visibility
        conditions: Rules for showing/hiding fields
    
    Example:
        create_conditional_form(
            "Product Configuration",
            "Configure your product settings",
            [
                {"name": "type", "type": "select", "label": "Product Type", "options": ["physical", "digital"]},
                {"name": "weight", "type": "number", "label": "Weight (kg)", "show_if": {"field": "type", "value": "physical"}},
                {"name": "download_url", "type": "text", "label": "Download URL", "show_if": {"field": "type", "value": "digital"}}
            ],
            [
                {"if": {"field": "type", "equals": "physical"}, "show": ["weight", "shipping"]},
                {"if": {"field": "type", "equals": "digital"}, "show": ["download_url", "license"]}
            ]
        )
    """
    return {
        "interaction_type": "conditional_form",
        "title": title,
        "description": description,
        "fields": fields,
        "conditions": conditions
    }

def create_wizard_flow(
    title: str,
    description: str,
    steps: List[Dict[str, Any]],
    allow_back: bool,
    show_progress: bool
) -> Dict[str, Any]:
    """
    Create a multi-step wizard interface.
    
    Perfect for: Complex setups, guided processes, step-by-step configurations
    
    Args:
        title: Wizard title
        description: Overall purpose
        steps: List of steps with fields and validation
        allow_back: Allow going back to previous steps
        show_progress: Show progress indicator
    
    Example:
        create_wizard_flow(
            "Product Setup Wizard",
            "Set up your new product in 3 easy steps",
            [
                {
                    "id": "basic",
                    "title": "Basic Information",
                    "fields": [
                        {"name": "name", "type": "text", "label": "Product Name", "required": True}
                    ]
                },
                {
                    "id": "pricing",
                    "title": "Pricing",
                    "fields": [
                        {"name": "price", "type": "number", "label": "Price", "required": True}
                    ]
                }
            ],
            True,
            True
        )
    """
    return {
        "interaction_type": "wizard_flow",
        "title": title,
        "description": description,
        "steps": steps,
        "allow_back": allow_back,
        "show_progress": show_progress
    }

if __name__ == "__main__":
    print("Advanced Agent Interaction Tools loaded successfully!")
    print("\nExample Searchable Selection:")
    print(json.dumps(create_searchable_selection(
        "Select Products",
        "Choose products to update",
        [
            {"id": "1", "name": "iPhone 15", "description": "Latest smartphone", "price": "$999"},
            {"id": "2", "name": "MacBook Pro", "description": "Professional laptop", "price": "$2499"}
        ],
        ["name", "description"],
        True,
        True,
        True,
        None
    )))
