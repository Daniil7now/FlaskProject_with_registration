from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash
import pandas as pd
import numpy as np
import sqlite3
import os
import io

DB_FILE = "database.db"

from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps

app = Flask(__name__)
app.secret_key = 'supersecretkey'


# Pandas Settings
pd.set_option("display.max_rows", 500)
pd.set_option("display.max_columns", 500)

# Constants
TABLE_CUSTOMERS = "customers"
TABLE_INVOICES = "invoices"
TABLE_PRODUCTS = "products"
TABLE_EXPENSES = "expenses"
COLUMN_CUSTOMER = "Customer"
COLUMN_PRODUCT = "Product"
COLUMN_INVOICE_NO = "Invoice_No"
COLUMN_QUANTITY = "Quantity"
COLUMN_SALES_AMOUNT = "Sales_Amount"


#here we're protecting routs
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

#Table for users
def create_users_table():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL
        )
    ''')
    conn.commit()
    conn.close()

# Here we load CSV in database
def load_csv(file, table_name):
    df = pd.read_csv(
        file,
        thousands=",",
        decimal=".",
        na_values=["NA", "na", "N/A", "n/a", ""],
    )
    for col in df.columns:
        try:
            df[col] = pd.to_numeric(df[col])
        except ValueError:
            pass

    if "Product" in df.columns:
        df["Product"] = df["Product"].str.strip()

    conn = sqlite3.connect(DB_FILE)
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()

from flask import render_template, request

def load_excel(file, table_name):
    df = pd.read_excel(file)

    if "Product" in df.columns:
        df["Product"] = df["Product"].str.strip()

    conn = sqlite3.connect(DB_FILE)
    df.to_sql(table_name, conn, if_exists="replace", index=False)
    conn.close()

def load_data(file, table_name, required_columns):
    try:
        # Here we delete old table if is exist
        try:
            conn = sqlite3.connect(DB_FILE)
            c = conn.cursor()
            c.execute(f"DROP TABLE IF EXISTS {table_name}")
            conn.commit()
            conn.close()
        except sqlite3.Error as e:
            return f"An error occurred: {e.args[0]}"

        file_extension = file.filename.split(".")[-1].lower()

        if file_extension == "csv":
            df = load_csv(file, table_name)
        elif file_extension in ["xlsx", "xls"]:
            df = load_excel(file, table_name)
        else:
            return "Unsupported file type"

        # Table check
        if table_name == "expenses":
            if (
                "Allocate_To" in df.columns
                and "Allocate_By_Tran" in df.columns
                and "Allocate_By_Value" in df.columns
            ):
                allocation_columns_pre_filled = True
            else:
                allocation_columns_pre_filled = False

        # Check columns
        if not set(required_columns).issubset(df.columns):
            missing = set(required_columns) - set(df.columns)
            return f"Missing required columns: {missing}"

        return {"df": df, "allocation_columns_pre_filled": allocation_columns_pre_filled}

    except Exception as e:
        return f"Error when trying to read the file: {e}"

def load_data_from_db(table_name):
    try:
        conn = sqlite3.connect(DB_FILE)
        df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
        conn.close()
        return df
    except Exception as e:
        return f"Error loading data from database: {e}"

def merge_data():
    try:
        # Opening a connection to the database
        conn = sqlite3.connect(DB_FILE)

        # Getting a list of columns from the customers and products tables
        customers_columns = pd.read_sql_query(
            f"PRAGMA table_info({TABLE_CUSTOMERS})", conn
        )["name"].tolist()
        products_columns = pd.read_sql_query(
            f"PRAGMA table_info({TABLE_PRODUCTS})", conn
        )["name"].tolist()

        # We remove duplicate key fields (Customer and Product) so that they are not duplicated
        if COLUMN_CUSTOMER in customers_columns:
            customers_columns.remove(COLUMN_CUSTOMER)
        if COLUMN_PRODUCT in products_columns:
            products_columns.remove(COLUMN_PRODUCT)

        # Creating column rows for an SQL query
        customers_columns_str = ", ".join(
            [f'customers."{col}"' for col in customers_columns]
        )
        products_columns_str = ", ".join(
            [f'products."{col}"' for col in products_columns]
        )

        # All the necessary columns for SELECT
        all_columns_str = ", ".join(
            filter(None, ["invoices.*", customers_columns_str, products_columns_str])
        )

        # The SQL join query itself (LEFT JOIN)
        query = f"""
            SELECT {all_columns_str}
            FROM invoices
            LEFT JOIN customers ON invoices.{COLUMN_CUSTOMER} = customers.{COLUMN_CUSTOMER}
            LEFT JOIN products ON invoices.{COLUMN_PRODUCT} = products.{COLUMN_PRODUCT}
        """

        # Performing the merge
        merged_df = pd.read_sql_query(query, conn)

        # Closing the connection
        conn.close()

        return merged_df

    except Exception as e:
        return f"Error merging tables: {e}"

def calculate_cost(df):
    try:
        if 'Product_Cost' in df.columns:
            df['Cost_Amount'] = df['Quantity'] * df['Product_Cost']
        elif 'Cost_%' in df.columns:
            df['Cost_Amount'] = df['Sales_Amount'] * df['Cost_%']
        else:
            df['Cost_Amount'] = 0
    except Exception as e:
        print(f"Error in cost calculation: {e}")
        df['Cost_Amount'] = 0
    return df

def get_all_columns(df_list):
    all_columns = []
    for df in df_list:
        if df is not None:
            non_numeric_cols = df.select_dtypes(exclude="number").columns.tolist()
            non_empty_cols = [
                col
                for col in non_numeric_cols
                if (df[col].dropna() != pd.Timestamp(0)).any()
            ]
            all_columns.extend(non_empty_cols)
    return list(set(all_columns))

def process_expenses(df, expenses_df):
    """Adds new expense columns and distributes them."""
    try:
        unique_expenses = expenses_df["Expense"].unique()
        zero_data = pd.DataFrame(0, index=df.index, columns=unique_expenses)
        df = pd.concat([df, zero_data], axis=1)

        for expense_name in unique_expenses:
            expense_rows = expenses_df[expenses_df["Expense"] == expense_name]
            for _, row in expense_rows.iterrows():
                df = allocate_expense(df, row)
        return df
    except Exception as e:
        print(f"Expense processing error: {e}")
        return df

def allocate_expense(df, row):
    """Distributes the amounts of a single expense according to the rules."""
    try:
        expense_name = row["Expense"]
        allocations = row["Allocate_To"].split(";")
        by_tran = row["Allocate_By_Tran"] / len(allocations)
        by_value = row["Allocate_By_Value"] / len(allocations)
        total_amount = row["Amount"]

        amount_by_tran = total_amount * by_tran
        amount_by_value = total_amount * by_value

        for allocation in allocations:
            df = allocate_based_on_rules(
                df, allocation.strip(), amount_by_tran, amount_by_value, expense_name
            )
    except Exception as e:
        print(f"Cost allocation error: {e}")
    return df

def allocate_based_on_rules(df, allocation, amount_by_tran, amount_by_value, expense_name):
    """Applies allocation rules with support for AND (;) and OR (|) logic."""
    try:
        if allocation.lower() == "all":
            if not df.empty:
                df[expense_name] += (
                    amount_by_tran / len(df)
                    + amount_by_value * (df["Sales_Amount"] / df["Sales_Amount"].sum())
                )
        else:
            conditions = allocation.split(";")
            temp_df = df.copy()

            for condition in conditions:
                if "=" in condition:
                    key, values = condition.split("=")
                    key = key.strip()
                    value_list = [v.strip() for v in values.split("|")]

                    if key in df.columns:
                        temp_df = temp_df[temp_df[key].isin(value_list)]
                    else:
                        print(f"Warning: column '{key}' not found in DataFrame. Skipping condition.")
                else:
                    print(f"Invalid condition format: {condition}")

            if not temp_df.empty:
                total_sales = temp_df["Sales_Amount"].sum() or 1  # prevent division by zero
                df.loc[temp_df.index, expense_name] += (
                    amount_by_tran / len(temp_df)
                    + amount_by_value * (df.loc[temp_df.index, "Sales_Amount"] / total_sales)
                )
            else:
                print(f"Warning: no rows matched the condition for '{expense_name}'.")
    except Exception as e:
        print(f"Error applying allocation rules: {e}")
    return df

def calculate_totals(df, expenses_df):
    try:
        expense_columns = []

        for expense_name in expenses_df["Expense"].unique():
            weighted_column_name = f"{expense_name}_Weighted"
            if weighted_column_name in df.columns:
                expense_columns.append(weighted_column_name)
            else:
                expense_columns.append(expense_name)

        total_expense = df[expense_columns].sum(axis=1)
        df["Total_Expense"] = total_expense.round(2)
        df["Net_Profit"] = (df["Sales_Amount"] - df["Total_Expense"] - df["Cost_Amount"]).round(2)

    except Exception as e:
        print(f"Error in calculating totals: {e}")
    return df

# Add a row of totals (sums by numeric columns)
def append_totals(df):
    try:
        totals = df.select_dtypes(include=np.number).sum().rename("Total")
        df.index = df.index.astype(str)  # Индексы строк в строковый тип
        df = pd.concat([df, pd.DataFrame(totals).T])
    except Exception as e:
        print(f"Error when adding totals: {e}")
    return df

# Delete completely empty columns
def remove_empty_columns(df):
    df = df.dropna(how="all", axis=1)
    return df

# Converting a DataFrame to a CSV string or binary Excel file
def to_csv_xlsx(df, extension):
    if extension == "csv":
        return df.to_csv(index=False)
    elif extension in ["xlsx", "xls"]:
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            df.to_excel(writer, index=False)
        output.seek(0)
        return output

# Creating a download link in Flash by sending a file
def download_file(df, filename, extension):
    if extension == "csv":
        csv_data = df.to_csv(index=False)
        return send_file(
            io.BytesIO(csv_data.encode('utf-8')),
            mimetype="text/csv",
            as_attachment=True,
            download_name=filename
        )
    elif extension in ["xlsx", "xls"]:
        excel_data = to_csv_xlsx(df, extension)
        return send_file(
            excel_data,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            as_attachment=True,
            download_name=filename
        )
    else:
        return "Unsupported file type", 400

# We check the uploaded files via request.files
def check_uploaded_files():
    customers_file = request.files.get('customers')
    invoices_file = request.files.get('invoices')
    products_file = request.files.get('products')
    expenses_file = request.files.get('expenses')

    # Determining the file extension (if there is a file)
    file_extension = None
    if customers_file:
        file_extension = customers_file.filename.split('.')[-1]

    return customers_file, invoices_file, products_file, expenses_file, file_extension

# Report generation
def generate_report(df, allocation_factor, report_type, expenses_df):
    sum_columns = [
        "Quantity",
        "Sales_Amount",
        "Cost_Amount",
        "Total_Expense",
        "Net_Profit",
    ]

    try:
        if report_type == "Summary":
            if allocation_factor.lower() == "all":
                report_df = df.loc["Total", sum_columns].to_frame().T
                report_df.index = ["Grand Total"]
            else:
                report_df = df.groupby(allocation_factor)[sum_columns].sum()
                report_df.loc["Grand Total"] = report_df.sum()

            report_df.rename(columns={col: f"Sum of {col}" for col in sum_columns}, inplace=True)

        elif report_type == "Detailed":
            relevant_cols = sum_columns + list(expenses_df["Expense"].unique())
            numeric_cols = df.select_dtypes(include=[np.number]).columns.intersection(relevant_cols)

            if allocation_factor.lower() == "all":
                report_df = df[numeric_cols].copy()
                report_df = report_df.groupby(df.index).sum()
            else:
                report_df = df.groupby(allocation_factor)[numeric_cols].sum()

            if "Total" not in report_df.index:
                report_df.loc["Grand Total"] = report_df.sum()

            report_df.rename(columns={col: f"Sum of {col}" for col in report_df.columns}, inplace=True)

        # Rounding the numeric columns to integers
        numeric_columns = report_df.select_dtypes(include=[np.number]).columns
        for col in numeric_columns:
            report_df[col] = report_df[col].round(0).astype(int)

        display_df = report_df.copy()

        if allocation_factor.lower() != "all":
            report_df = report_df.reset_index(drop=False)

    except Exception as e:
        print(f"Error generating the report: {e}")
        report_df, display_df = None, None

    return report_df, display_df

# Adding missing products to the product table
def add_missing_products_to_products_table(products_table_name, missing_products, conn):
    try:
        # Creating a Data Frame with missing products
        missing_products_df = pd.DataFrame(
            {
                "Product": list(missing_products),
                "Product_Cost": 0,  # Can be changed to np.nan if necessary.
            }
        )

        # Adding new products to the table
        missing_products_df.to_sql(products_table_name, conn, if_exists="append", index=False)

        # Updating products (if you need to return updated data)
        products_df = load_data_from_db(products_table_name)
        return products_df

    except Exception as e:
        print(f"Error when adding products: {e}")
        return None

def apply_expense_weights(df, expenses_df, enabled_expenses):
    weighted_df = df.copy()

    for i, row in expenses_df.iterrows():
        if (
            "Weights" in row
            and pd.notna(row["Weights"])
            and enabled_expenses.get(i, False)
        ):
            weight_info = eval(row["Weights"])
            column_name = weight_info["column_name"]
            weight_mapping = weight_info["weights"]

            # 1. Calculate the weight of each row
            weighted_df[f"{row['Expense']}_Weight"] = (
                weighted_df[column_name].map(weight_mapping).fillna(0)
            )

            # 2. Calculating the weight for the string
            weighted_df[f"{row['Expense']}_xWeight"] = (
                weighted_df[row["Expense"]] * weighted_df[f"{row['Expense']}_Weight"]
            )

            # 3. Counting the total weight
            total_xWeight = weighted_df[f"{row['Expense']}_xWeight"].sum()

            # 4. Calculating the Share of each row
            if total_xWeight != 0:
                weighted_df[f"{row['Expense']}_Share"] = weighted_df[f"{row['Expense']}_xWeight"] / total_xWeight
            else:
                weighted_df[f"{row['Expense']}_Share"] = 0

            # Correcting a possible summation error
            difference = 1.0 - weighted_df[f"{row['Expense']}_Share"].sum()
            if not weighted_df[f"{row['Expense']}_Share"].isnull().all():
                weighted_df.loc[
                    weighted_df[f"{row['Expense']}_Share"].idxmax(),
                    f"{row['Expense']}_Share",
                ] += difference

            # 5. Calculating weighted expenses
            weighted_df[f"{row['Expense']}_Weighted"] = (
                weighted_df[f"{row['Expense']}_Share"] * row["Amount"]
            )

            # Replacing the original column with a weighted value
            weighted_df[row["Expense"]] = weighted_df[f"{row['Expense']}_Weighted"]

            # Removing the temporary columns
            weighted_df.drop(
                columns=[
                    f"{row['Expense']}_Weight",
                    f"{row['Expense']}_xWeight",
                    f"{row['Expense']}_Share",
                ],
                inplace=True,
            )

    return weighted_df

# app.py

from flask import Flask, render_template, request, redirect, url_for, session, send_file, flash
import pandas as pd
import numpy as np
import sqlite3
import os
from io import BytesIO

app = Flask(__name__)
app.secret_key = 'your_secret_key'

UPLOAD_FOLDER = 'uploads'
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

DB_FILE = 'database.db'


# Auxiliary functions

def init_db():
    conn = sqlite3.connect(DB_FILE)
    conn.close()

def save_uploaded_file(file_storage, filename):
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file_storage.save(file_path)
    return file_path

def load_data_from_file(filepath):
    if filepath.endswith('.csv'):
        return pd.read_csv(filepath)
    elif filepath.endswith('.xlsx'):
        return pd.read_excel(filepath)
    else:
        raise ValueError("Unsupported file type")

def save_to_db(df, table_name):
    conn = sqlite3.connect(DB_FILE)
    df.to_sql(table_name, conn, if_exists='replace', index=False)
    conn.close()

def load_data_from_db(table_name):
    conn = sqlite3.connect(DB_FILE)
    df = pd.read_sql_query(f"SELECT * FROM {table_name}", conn)
    conn.close()
    return df

def add_missing_products_to_products_table(products_table_name, missing_products):
    conn = sqlite3.connect(DB_FILE)
    missing_products_df = pd.DataFrame({
        'Product': list(missing_products),
        'Product_Cost': 0
    })
    missing_products_df.to_sql(products_table_name, conn, if_exists='append', index=False)
    conn.close()

def merge_data(customers_table, invoices_table, products_table):
    customers_df = load_data_from_db(customers_table)
    invoices_df = load_data_from_db(invoices_table)
    products_df = load_data_from_db(products_table)

    merged = pd.merge(invoices_df, customers_df, on="Customer", how="left")
    merged = pd.merge(merged, products_df, on="Product", how="left")
    return merged

# Routes

@app.route('/upload', methods=["POST"])
@login_required
def upload():
    file = request.files['file']
    table_name = request.form['table_name']
    if file:
        load_csv(file, table_name)
        return f"The file is uploaded and saved to the table '{table_name}'!"
    return "Error when uploading."

@app.route('/', methods=['GET', 'POST'])
@login_required
def index():
    if request.method == 'POST':

        customers_file = request.files['customers_file']
        invoices_file = request.files['invoices_file']
        products_file = request.files['products_file']
        expenses_file = request.files['expenses_file']

        customers_path = save_uploaded_file(customers_file, 'customers.' + customers_file.filename.split('.')[-1])
        invoices_path = save_uploaded_file(invoices_file, 'invoices.' + invoices_file.filename.split('.')[-1])
        products_path = save_uploaded_file(products_file, 'products.' + products_file.filename.split('.')[-1])
        expenses_path = save_uploaded_file(expenses_file, 'expenses.' + expenses_file.filename.split('.')[-1])

        customers_df = load_data_from_file(customers_path)
        invoices_df = load_data_from_file(invoices_path)
        products_df = load_data_from_file(products_path)
        expenses_df = load_data_from_file(expenses_path)

        customers_required_columns = ["Customer"]
        invoices_required_columns = ["Invoice_No", "Customer", "Product", "Quantity", "Sales_Amount"]
        products_required_columns = ["Product"]
        expenses_required_columns = ["Expense", "Amount"]

        for col in customers_required_columns:
            if col not in customers_df.columns:
                flash(f"Customers file missing required column: {col}")
                return redirect(url_for('index'))

        for col in invoices_required_columns:
            if col not in invoices_df.columns:
                flash(f"Invoices file missing required column: {col}")
                return redirect(url_for('index'))

        for col in products_required_columns:
            if col not in products_df.columns:
                flash(f"Products file missing required column: {col}")
                return redirect(url_for('index'))

        for col in expenses_required_columns:
            if col not in expenses_df.columns:
                flash(f"Expenses file missing required column: {col}")
                return redirect(url_for('index'))

        # Сохраняем в БД
        save_to_db(customers_df, "customers")
        save_to_db(invoices_df, "invoices")
        save_to_db(products_df, "products")
        save_to_db(expenses_df, "expenses")

        return redirect(url_for('allocation'))

    return render_template('index.html')


@app.route('/allocation', methods=['GET', 'POST'])
@login_required
def allocation():
    customers_df = load_data_from_db("customers")
    invoices_df = load_data_from_db("invoices")
    products_df = load_data_from_db("products")
    expenses_df = load_data_from_db("expenses")


    invoices_products = set(invoices_df["Product"].unique())
    products_products = set(products_df["Product"].unique())
    mismatch_products = invoices_products - products_products

    merged_df = merge_data("customers", "invoices", "products")
    merged_df = calculate_cost(merged_df)

    all_columns = list(set(customers_df.columns) | set(invoices_df.columns) | set(products_df.columns))
    all_columns = ["All"] + [col for col in all_columns if col.lower() != "date"]

    if request.method == 'POST':
        allocation_rules = []
        for i in range(len(expenses_df)):
            rule = request.form.get(f"rule_{i}", "all")
            tran_percent = float(request.form.get(f"tran_{i}", 50)) / 100.0

            weight_flag = request.form.get(f"weight_{i}")
            weight_flag = True if weight_flag else False

            # update DataFrame
            expenses_df.at[i, "Allocate_To"] = rule
            expenses_df.at[i, "Allocate_By_Tran"] = tran_percent
            expenses_df.at[i, "Allocate_By_Value"] = 1 - tran_percent
            expenses_df.at[i, "Apply_Weight"] = weight_flag

        # Update expenses dataframe
        for i, rules in enumerate(allocation_rules):
            expenses_df.at[i, "Allocate_To"] = rules["Allocate_To"]
            expenses_df.at[i, "Allocate_By_Tran"] = rules["Allocate_By_Tran"]
            expenses_df.at[i, "Allocate_By_Value"] = rules["Allocate_By_Value"]

        # Save updated expenses to database
        save_to_db(expenses_df, "expenses")

        # Set a session flag that allocation rules are filled
        session['allocation_done'] = True

        return redirect(url_for('processing'))

    return render_template('allocation.html', expenses=expenses_df.to_dict(orient='records'), all_columns=all_columns)


@app.route('/processing')
@login_required
def processing():
    if not session.get('allocation_done'):
        return redirect(url_for('allocation'))

    merged_df = merge_data("customers", "invoices", "products")
    merged_df = calculate_cost(merged_df)
    expenses_df = load_data_from_db("expenses")

    # Process expenses
    processed_df = process_expenses(merged_df, expenses_df)

    # Here you could apply expense weights if needed (if you have such a feature)
    # processed_df = apply_expense_weights(processed_df, expenses_df)

    processed_df = calculate_totals(processed_df, expenses_df)
    processed_df = append_totals(processed_df)
    processed_df = remove_empty_columns(processed_df)

    # Save processed DataFrame if you want
    save_to_db(processed_df, "processed_data")

    return render_template('processing.html', tables=[processed_df.to_html(classes='data', header="true")])


@app.route('/download/<filename>')
@login_required
def download(filename):
    filepath = os.path.join(UPLOAD_FOLDER, filename)
    return send_file(filepath, as_attachment=True)

@app.route('/download/processed-excel')
@login_required
def download_processed_excel():
    df = load_data_from_db("processed_data")
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False)
    output.seek(0)
    return send_file(
        output,
        as_attachment=True,
        download_name="processed_data.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )


# Starting the server

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM users WHERE email = ?", (email,))
        existing_user = cursor.fetchone()

        if existing_user:
            flash('User already exists.')
            return redirect(url_for('register'))

        # If not, create a new one.
        password_hash = generate_password_hash(password)
        cursor.execute("INSERT INTO users (email, password_hash) VALUES (?, ?)", (email, password_hash))
        conn.commit()
        conn.close()

        flash('Registration successful. Please log in.')
        return redirect(url_for('login'))

    return render_template('register.html')

#route to login
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form['email']
        password = request.form['password']

        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        cursor.execute("SELECT id, password_hash FROM users WHERE email = ?", (email,))
        user = cursor.fetchone()
        conn.close()

        if user and check_password_hash(user[1], password):
            session['user_id'] = user[0]
            session['user_email'] = email
            flash('Logged in successfully.')
            return redirect(url_for('index'))
        else:
            flash('Invalid email or password.')

    return render_template('login.html')

#route for logout
@app.route('/logout')
@login_required
def logout():
    session.clear()
    flash('You have been logged out.')
    return redirect(url_for('login'))

if __name__ == '__main__':
    create_users_table()
    app.run(debug=True)