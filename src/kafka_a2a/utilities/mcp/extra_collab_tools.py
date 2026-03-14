from typing import Dict, List, Any, Optional

def create_dashboard_builder(
    title: str,
    description: str,
    available_widgets: List[Dict[str, Any]],
    current_layout: List[Dict[str, Any]],
    allow_custom_widgets: bool
) -> Dict[str, Any]:
    """
    Create a dashboard builder interface for users to customize their dashboards.
    
    Args:
        title: Dashboard builder title
        description: Instructions for the user
        available_widgets: List of available widget types
        current_layout: Current dashboard layout
        allow_custom_widgets: Whether users can create custom widgets
    
    Returns:
        Dictionary with dashboard builder configuration
    """
    return {
        "interaction_type": "dashboard_builder",
        "title": title,
        "description": description,
        "available_widgets": available_widgets or [],
        "current_layout": current_layout or [],
        "allow_custom_widgets": allow_custom_widgets or False
    }

def create_master_detail_table(
    title: str,
    description: str,
    master_data: List[Dict[str, Any]],
    master_columns: List[Dict[str, Any]],
    detail_key: str,
    detail_columns: List[Dict[str, Any]],
    allow_editing: bool,
    allow_expansion: bool
) -> Dict[str, Any]:
    """
    Create a master-detail table for hierarchical data like purchase orders with line items.
    
    Args:
        title: Table title
        description: Table description
        master_data: Main table data (e.g., purchase orders)
        master_columns: Column definitions for master table
        detail_key: Key in master data that contains detail records
        detail_columns: Column definitions for detail table
        allow_editing: Whether to allow inline editing
        allow_expansion: Whether rows can be expanded to show details
    
    Returns:
        Dictionary with master-detail table configuration
    """
    return {
        "interaction_type": "master_detail_table",
        "title": title,
        "description": description,
        "master_data": master_data or [],
        "master_columns": master_columns or [],
        "detail_key": detail_key or "items",
        "detail_columns": detail_columns or [],
        "allow_editing": allow_editing or False,
        "allow_expansion": allow_expansion or True
    }

def create_alert_manager(
    title: str,
    description: str,
    available_triggers: List[Dict[str, Any]],
    available_actions: List[Dict[str, Any]],
    existing_alerts: List[Dict[str, Any]],
    allow_custom_triggers: bool
) -> Dict[str, Any]:
    """
    Create an alert/notification management interface.
    
    Args:
        title: Alert manager title
        description: Instructions for setting up alerts
        available_triggers: List of available trigger conditions
        available_actions: List of available alert actions
        existing_alerts: Current user alerts
        allow_custom_triggers: Whether users can create custom triggers
    
    Returns:
        Dictionary with alert manager configuration
    """
    return {
        "interaction_type": "alert_manager",
        "title": title,
        "description": description,
        "available_triggers": available_triggers or [],
        "available_actions": available_actions or [],
        "existing_alerts": existing_alerts or [],
        "allow_custom_triggers": allow_custom_triggers or False
    }

def create_task_assignment(
    title: str,
    description: str,
    task_details: Dict[str, Any],
    available_assignees: List[Dict[str, Any]],
    priority_levels: List[str],
    due_date_required: bool,
    allow_attachments: bool
) -> Dict[str, Any]:
    """
    Create a task assignment interface for workflow management.
    
    Args:
        title: Task assignment title
        description: Task description
        task_details: Details about the task to be assigned
        available_assignees: List of users who can be assigned
        priority_levels: Available priority levels
        due_date_required: Whether due date is required
        allow_attachments: Whether to allow file attachments
    
    Returns:
        Dictionary with task assignment configuration
    """
    return {
        "interaction_type": "task_assignment",
        "title": title,
        "description": description,
        "task_details": task_details or {},
        "available_assignees": available_assignees or [],
        "priority_levels": priority_levels or ["Low", "Medium", "High", "Urgent"],
        "due_date_required": due_date_required or False,
        "allow_attachments": allow_attachments or True
    }

def create_comment_thread(
    title: str,
    description: str,
    context_object: Dict[str, Any],
    existing_comments: List[Dict[str, Any]],
    allow_mentions: bool,
    allow_attachments: bool,
    thread_permissions: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Create a comment thread interface for contextual discussions.
    
    Args:
        title: Thread title
        description: Thread description
        context_object: Object the comments are related to
        existing_comments: Current comments in the thread
        allow_mentions: Whether users can mention others
        allow_attachments: Whether to allow file attachments
        thread_permissions: Who can read/write comments
    
    Returns:
        Dictionary with comment thread configuration
    """
    return {
        "interaction_type": "comment_thread",
        "title": title,
        "description": description,
        "context_object": context_object or {},
        "existing_comments": existing_comments or [],
        "allow_mentions": allow_mentions or True,
        "allow_attachments": allow_attachments or True,
        "thread_permissions": thread_permissions or {}
    }

def create_report_builder(
    title: str,
    description: str,
    available_data_sources: List[Dict[str, Any]],
    available_fields: List[Dict[str, Any]],
    available_filters: List[Dict[str, Any]],
    available_visualizations: List[str],
    saved_reports: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Create a report builder interface for custom reports.
    
    Args:
        title: Report builder title
        description: Instructions for building reports
        available_data_sources: Available data sources
        available_fields: Fields that can be included
        available_filters: Available filter options
        available_visualizations: Chart/visualization types
        saved_reports: User's saved reports
    
    Returns:
        Dictionary with report builder configuration
    """
    return {
        "interaction_type": "report_builder",
        "title": title,
        "description": description,
        "available_data_sources": available_data_sources or [],
        "available_fields": available_fields or [],
        "available_filters": available_filters or [],
        "available_visualizations": available_visualizations or ["table", "bar_chart", "line_chart", "pie_chart"],
        "saved_reports": saved_reports or []
    }

def create_data_visualization(
    title: str,
    description: str,
    chart_type: str,
    data: List[Dict[str, Any]],
    chart_config: Dict[str, Any],
    allow_interaction: bool,
    export_options: List[str]
) -> Dict[str, Any]:
    """
    Create a data visualization interface with interactive charts.
    
    Args:
        title: Visualization title
        description: Chart description
        chart_type: Type of chart (bar, line, pie, etc.)
        data: Chart data
        chart_config: Chart configuration options
        allow_interaction: Whether chart is interactive
        export_options: Available export formats
    
    Returns:
        Dictionary with data visualization configuration
    """
    return {
        "interaction_type": "data_visualization",
        "title": title,
        "description": description,
        "chart_type": chart_type or "bar",
        "data": data or [],
        "chart_config": chart_config or {},
        "allow_interaction": allow_interaction or True,
        "export_options": export_options or ["png", "pdf", "csv"]
    }

def create_timeline_activity(
    title: str,
    description: str,
    activities: List[Dict[str, Any]],
    time_range: Dict[str, str],
    group_by: str,
    allow_filtering: bool,
    show_details: bool
) -> Dict[str, Any]:
    """
    Create a timeline/activity feed interface.
    
    Args:
        title: Timeline title
        description: Timeline description
        activities: List of activities/events
        time_range: Start and end time for timeline
        group_by: How to group activities (day, week, month)
        allow_filtering: Whether to allow activity filtering
        show_details: Whether to show detailed activity info
    
    Returns:
        Dictionary with timeline configuration
    """
    return {
        "interaction_type": "timeline_activity",
        "title": title,
        "description": description,
        "activities": activities or [],
        "time_range": time_range or {},
        "group_by": group_by or "day",
        "allow_filtering": allow_filtering or True,
        "show_details": show_details or True
    }

def create_kanban_board(
    title: str,
    description: str,
    columns: List[Dict[str, Any]],
    cards: List[Dict[str, Any]],
    allow_drag_drop: bool,
    allow_card_editing: bool,
    column_limits: Dict[str, int]
) -> Dict[str, Any]:
    """
    Create a kanban board interface for visual task management.
    
    Args:
        title: Board title
        description: Board description
        columns: Kanban columns/stages
        cards: Task cards
        allow_drag_drop: Whether cards can be dragged between columns
        allow_card_editing: Whether cards can be edited inline
        column_limits: Maximum cards per column
    
    Returns:
        Dictionary with kanban board configuration
    """
    return {
        "interaction_type": "kanban_board",
        "title": title,
        "description": description,
        "columns": columns or [],
        "cards": cards or [],
        "allow_drag_drop": allow_drag_drop or True,
        "allow_card_editing": allow_card_editing or True,
        "column_limits": column_limits or {}
    }

def create_approval_workflow(
    title: str,
    description: str,
    workflow_item: Dict[str, Any],
    approval_steps: List[Dict[str, Any]],
    current_step: int,
    allow_comments: bool,
    allow_delegation: bool
) -> Dict[str, Any]:
    """
    Create an approval workflow interface for multi-step approvals.
    
    Args:
        title: Workflow title
        description: Workflow description
        workflow_item: Item requiring approval
        approval_steps: List of approval steps
        current_step: Current step in workflow
        allow_comments: Whether approvers can add comments
        allow_delegation: Whether approvers can delegate
    
    Returns:
        Dictionary with approval workflow configuration
    """
    return {
        "interaction_type": "approval_workflow",
        "title": title,
        "description": description,
        "workflow_item": workflow_item or {},
        "approval_steps": approval_steps or [],
        "current_step": current_step or 0,
        "allow_comments": allow_comments or True,
        "allow_delegation": allow_delegation or False
    }

def create_wizard_flow(
    title: str,
    description: str,
    steps: List[Dict[str, Any]],
    current_step: int,
    allow_skip: bool,
    existing_responses: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Create a wizard flow interface for multi-step guided processes like onboarding.
    
    Args:
        title: Wizard title
        description: Wizard description
        steps: List of wizard steps with fields
        current_step: Current step index (0-based)
        allow_skip: Whether users can skip the wizard
        existing_responses: Previously collected responses
    
    Returns:
        Dictionary with wizard flow configuration
    
    Example:
        create_wizard_flow(
            "User Onboarding",
            "Let's set up your profile and preferences",
            [
                {
                    "title": "Basic Information",
                    "description": "Tell us about yourself",
                    "fields": [
                        {"name": "name", "label": "Full Name", "type": "text", "required": True},
                        {"name": "email", "label": "Email", "type": "text", "required": True}
                    ]
                },
                {
                    "title": "Preferences",
                    "description": "Customize your experience",
                    "fields": [
                        {"name": "theme", "label": "Theme", "type": "select", "options": [
                            {"value": "light", "label": "Light"},
                            {"value": "dark", "label": "Dark"}
                        ]},
                        {"name": "notifications", "label": "Enable Notifications", "type": "checkbox"}
                    ]
                }
            ],
            0,
            True,
            {}
        )
    """
    return {
        "interaction_type": "wizard_flow",
        "title": title,
        "description": description,
        "steps": steps or [],
        "current_step": current_step or 0,
        "allow_skip": allow_skip or True,
        "existing_responses": existing_responses or {}
    }

# Usage examples and documentation
if __name__ == "__main__":
    print("Extra Collaboration Tools loaded successfully!")
    
    # Example: Master-Detail Table for Purchase Orders
    print("\nExample Master-Detail Table:")
    print(create_master_detail_table(
        "Purchase Orders Review",
        "Review purchase orders and their line items",
        [
            {
                "id": "PO001",
                "supplier": "Tech Supplies Inc",
                "total": "$2,450.00",
                "status": "Pending",
                "items": [
                    {"product": "Laptop", "qty": 2, "price": "$1200.00"},
                    {"product": "Mouse", "qty": 5, "price": "$50.00"}
                ]
            }
        ],
        [
            {"key": "id", "label": "PO Number", "type": "text"},
            {"key": "supplier", "label": "Supplier", "type": "text"},
            {"key": "total", "label": "Total", "type": "currency"},
            {"key": "status", "label": "Status", "type": "badge"}
        ],
        "items",
        [
            {"key": "product", "label": "Product", "type": "text"},
            {"key": "qty", "label": "Quantity", "type": "number"},
            {"key": "price", "label": "Price", "type": "currency"}
        ],
        False,
        True
    ))

    print("\nExample Wizard Flow:")
    print(create_wizard_flow(
        "User Onboarding Wizard",
        "Complete your profile setup in a few easy steps",
        [
            {
                "title": "Personal Information",
                "description": "Tell us about yourself",
                "fields": [
                    {"name": "full_name", "label": "Full Name", "type": "text", "required": True, "placeholder": "Enter your full name"},
                    {"name": "company", "label": "Company", "type": "text", "required": False, "placeholder": "Your company name"}
                ]
            },
            {
                "title": "System Preferences",
                "description": "Customize your experience",
                "fields": [
                    {"name": "role", "label": "Your Role", "type": "select", "required": True, "options": [
                        {"value": "admin", "label": "Administrator"},
                        {"value": "manager", "label": "Manager"},
                        {"value": "user", "label": "User"}
                    ]},
                    {"name": "notifications", "label": "Enable Email Notifications", "type": "checkbox", "required": False}
                ]
            }
        ],
        0,
        True,
        {}
    ))
