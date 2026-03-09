from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from flask_bcrypt import Bcrypt
from flask_wtf.csrf import CSRFProtect
import requests
import pytesseract
from PIL import Image, ImageFilter
import io
from functools import wraps
import re

app = Flask(__name__)
app.secret_key = 'your_secure_secret_key_here'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///food_advisor.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# CSRF配置
csrf = CSRFProtect(app)
app.config['WTF_CSRF_CHECK_DEFAULT'] = False

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)

# OCR配置
pytesseract.pytesseract.tesseract_cmd = r'/usr/local/bin/tesseract'  # Mac
# pytesseract.pytesseract.tesseract_cmd = r'C:\Program Files\Tesseract-OCR\tesseract.exe'  # Windows

API_URL = "https://api.siliconflow.cn/v1/chat/completions"
API_KEY = "sk-aboqzpwhpjgnqugduuunhhnymzcpozeynfbidlkemocjvtgb"


# 数据库模型
class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True)
    password_hash = db.Column(db.String(100))
    diseases = db.relationship('Disease', backref='user', lazy=True)
    points = db.Column(db.Integer, default=0)  # 添加 points 列，默认值为 0
    shopping_list = db.Column(db.Text, default='')  # 新增购物清单列


class Disease(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    disease_name = db.Column(db.String(100))
    allergies = db.Column(db.Text)
    medications = db.Column(db.Text)


class Merchant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True)
    password_hash = db.Column(db.String(100))
    products = db.relationship('Product', backref='merchant', lazy=True)


class Product(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    merchant_id = db.Column(db.Integer, db.ForeignKey('merchant.id'))
    name = db.Column(db.String(100))
    ingredients = db.Column(db.Text)
    nutritional_info = db.Column(db.Text)
    food_category = db.Column(db.String(100))
    price = db.Column(db.Float)  # 新增价格字段


# 在应用上下文里创建所有表
with app.app_context():
    # 这里可以选择删除原数据库文件，或者使用数据库迁移工具（如 Flask-Migrate）来更新表结构
    # 如果你想直接删除数据库文件重新创建，可手动删除 food_advisor.db 文件，然后运行以下代码
    db.create_all()


# 认证装饰器
# 认证装饰器
def login_required(role='user'):
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if role == 'user' and 'user_id' not in session:
                return redirect(url_for('combined_login'))
            if role == 'merchant' and 'merchant_id' not in session:
                return redirect(url_for('combined_login'))
            return f(*args, **kwargs)

        return wrapped

    return decorator


def ocr_process(image):
    try:
        img = Image.open(io.BytesIO(image.read()))
        img = img.convert('L').filter(ImageFilter.MedianFilter())
        return pytesseract.image_to_string(img, lang='chi_sim')
    except Exception as e:
        raise Exception(f"OCR处理失败: {str(e)}")


def filter_ocr_result(result):
    prompt = f"请对以下OCR识别结果进行过滤和修正：{result}"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }

    try:
        response = requests.post(API_URL, json=data, headers=headers)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        return result  # 如果AI处理失败，返回原始结果


@app.route('/login', methods=['GET', 'POST'])
def combined_login():
    if request.method == 'POST':
        user_type = request.form['user_type']
        identifier = request.form['identifier']
        password = request.form['password']

        if user_type == 'user':
            user = User.query.filter_by(username=identifier).first()
            if user and bcrypt.check_password_hash(user.password_hash, password):
                session['user_id'] = user.id
                return redirect(url_for('user_dashboard'))
        elif user_type == 'merchant':
            merchant = Merchant.query.filter_by(name=identifier).first()
            if merchant and bcrypt.check_password_hash(merchant.password_hash, password):
                session['merchant_id'] = merchant.id
                return redirect(url_for('merchant_dashboard'))

        return render_template('combined_login.html', error="无效的凭证")
    return render_template('combined_login.html')


# ------------------ 用户端路由 -------------------
@app.route('/user/register', methods=['GET', 'POST'])
def user_register():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if User.query.filter_by(username=username).first():
            return render_template('user_register.html', error="用户名已存在")
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        user = User(username=username, password_hash=hashed_pw)
        db.session.add(user)
        db.session.commit()
        return redirect(url_for('combined_login'))
    return render_template('user_register.html')


@app.route('/user/dashboard')
@login_required('user')
def user_dashboard():
    user = User.query.get(session['user_id'])
    shopping_items = []
    total_amount = 0.0
    if user.shopping_list:
        item_names = list(set(user.shopping_list.split(',')))
        shopping_items = Product.query.filter(Product.name.in_(item_names)).all()
        total_amount = sum(p.price for p in shopping_items)
    return render_template('user_dashboard.html',
                           user=user,
                           shopping_items=shopping_items,
                           total_amount=total_amount)


@app.route('/api/user/diseases', methods=['POST', 'DELETE'])
@csrf.exempt
@login_required('user')
def manage_diseases():
    if request.method == 'POST':
        try:
            data = request.get_json()
            if not data or 'disease_name' not in data:
                return jsonify({"error": "疾病名称不能为空"}), 400

            disease = Disease(
                user_id=session['user_id'],
                disease_name=data['disease_name'],
                allergies=data.get('allergies', ''),
                medications=data.get('medications', '')
            )
            db.session.add(disease)
            db.session.commit()
            return jsonify({"message": "记录已添加", "id": disease.id}), 201
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": f"添加记录失败: {str(e)}"}), 500

    if request.method == 'DELETE':
        try:
            disease = Disease.query.filter_by(id=request.json['id'], user_id=session['user_id']).first()
            if not disease:
                return jsonify({"error": "记录未找到"}), 404
            db.session.delete(disease)
            db.session.commit()
            return jsonify({"message": "记录已删除"})
        except Exception as e:
            db.session.rollback()
            return jsonify({"error": f"删除记录失败: {str(e)}"}), 500


# ------------------ 商家端路由 -------------------
@app.route('/merchant/register', methods=['GET', 'POST'])
def merchant_register():
    if request.method == 'POST':
        name = request.form['name']
        password = request.form['password']
        if Merchant.query.filter_by(name=name).first():
            return render_template('merchant_register.html', error="商家名称已存在")
        hashed_pw = bcrypt.generate_password_hash(password).decode('utf-8')
        merchant = Merchant(name=name, password_hash=hashed_pw)
        db.session.add(merchant)
        db.session.commit()
        return redirect(url_for('combined_login'))
    return render_template('merchant_register.html')


@app.route('/merchant/dashboard')
@login_required('merchant')
def merchant_dashboard():
    merchant = Merchant.query.get(session['merchant_id'])
    return render_template('merchant_dashboard.html', merchant=merchant)


@app.route('/merchant/products', methods=['POST'])
@csrf.exempt
@login_required('merchant')
def add_product():
    try:
        # 获取表单数据
        name = request.form.get('name')
        price = float(request.form.get('price', 0))  # 获取价格
        ingredients = request.form.get('ingredients', '')

        # 处理图片上传
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file.filename != '':
                ingredients = ocr_process(image_file)
                ingredients = filter_ocr_result(ingredients)  # 交给AI过滤

        # 验证必填字段
        if not name or not ingredients:
            return jsonify({"error": "商品名称和成分不能为空"}), 400

        # 通过AI分析食物类别
        food_category = analyze_food_category(ingredients)

        # 创建商品记录
        product = Product(
            merchant_id=session['merchant_id'],
            name=name,
            price=price,  # 添加价格
            ingredients=ingredients,
            nutritional_info="待补充",
            food_category=food_category
        )
        db.session.add(product)
        db.session.commit()
        return jsonify({"message": "商品已添加", "id": product.id}), 201

    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"添加商品失败: {str(e)}"}), 500


def analyze_food_category(ingredients):
    prompt = f"""请从以下预设分类中选择最匹配的：谷类、水果、乳制品、蛋白质...
        成分：{ingredients}
        只需回答分类名称，不要解释"""
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }

    try:
        response = requests.post(API_URL, json=data, headers=headers)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except Exception as e:
        return "未知类别"


@app.route('/merchant/products/<int:id>', methods=['DELETE'])
@csrf.exempt
@login_required('merchant')
def delete_product(id):
    try:
        product = Product.query.filter_by(id=id, merchant_id=session['merchant_id']).first()
        if not product:
            return jsonify({"error": "商品未找到"}), 404
        db.session.delete(product)
        db.session.commit()
        return jsonify({"message": "商品已删除"})
    except Exception as e:
        db.session.rollback()
        return jsonify({"error": f"删除商品失败: {str(e)}"}), 500


# ------------------ 公共路由 -------------------
@app.route('/user/logout')
@login_required('user')
def user_logout():
    session.pop('user_id', None)
    return redirect(url_for('combined_login'))


@app.route('/merchant/logout')
@login_required('merchant')
def merchant_logout():
    session.pop('merchant_id', None)
    return redirect(url_for('combined_login'))


# ... 已有代码 ...

def analyze_food(user_info, food_info):
    # 获取所有商家的商品信息
    all_products = Product.query.all()
    all_product_names = [p.name for p in all_products]

    # 放宽商品推荐条件
    keywords = re.findall(r'\w+', food_info)
    query = Product.query
    for keyword in keywords:
        query = query.filter(Product.ingredients.contains(keyword))
    products = query.all()
    product_names = [p.name for p in products]

    # 根据食物类别进行推荐
    food_category = analyze_food_category(food_info)
    category_products = Product.query.filter(Product.food_category == food_category).all()
    category_product_names = [p.name for p in category_products]

    # 去除输入的食物本身
    if food_info in product_names:
        product_names.remove(food_info)
    if food_info in category_product_names:
        category_product_names.remove(food_info)

    # 合并推荐商品
    recommended_products = [{"id": p.id, "name": p.name, "price": p.price} for p in products]

    # 解析推荐食物类别
    prompt = f"""作为专业营养师分析：
    用户健康档案：{user_info}
    待评估食物：{food_info}

    所有商品信息：{', '.join(all_product_names)}
    推荐商品：{recommended_products}

    请按格式回答：
    1. 是否适合食用：[是/否]
    2. 原因分析：（详细说明对用户疾病的影响，特别是{food_info}的潜在风险）
    ⌈3. 推荐商品：（必须严格使用以下现有商品名称：{', '.join(all_product_names)}，且绝对不要包含用户正在查询的{food_info}，用顿号分隔）
    4. 推荐食物类别：（列出具体类别名称，不要包含商品，且不要包含{food_info}所属类别）⌋
    5. 饮食建议：（详细说明为什么要避免{food_info}）"""

    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    data = {
        "model": "deepseek-ai/DeepSeek-R1-Distill-Qwen-7B",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3
    }

    try:
        response = requests.post(API_URL, json=data, headers=headers)
        response.raise_for_status()
        analysis_result = response.json()['choices'][0]['message']['content']

        # 提取是否适合食用
        suitable = analysis_result.split("1. 是否适合食用：")[1].split("2. 原因分析：")[0].strip().replace(" ", "")

        # 提取推荐商品
        try:
            start_index = analysis_result.index("3. 推荐商品：") + len("3. 推荐商品：")
            end_index = analysis_result.index("4. 推荐食物类别：")
            recommended_products_str = analysis_result[start_index:end_index].strip()
            recommended_products = recommended_products_str.split("、") if recommended_products_str != "无" else []
        except ValueError:
            recommended_products = []

        # 提取推荐食物类别（新增异常处理）
        try:
            category_part = analysis_result.split("4. 推荐食物类别：")[1]
            recommended_categories_str = category_part.split("5. 饮食建议：")[0].strip()
            recommended_categories = recommended_categories_str.split("、") if recommended_categories_str != "无" else []
        except (IndexError, ValueError):
            recommended_categories = []  # 确保变量被初始化

        # 如果不适合食用，过滤掉推荐商品中与待评估食物相同的商品
        if suitable == "否":
            # 精确匹配要排除的食物名称（支持中英文括号）
            food_name = re.sub(r'[$（].*?[$）]', '', food_info).strip()
            recommended_products = [p for p in recommended_products if p.lower() != food_name.lower()]

            # 同时过滤推荐类别中的商品
            category_products = Product.query.filter(Product.food_category.in_(recommended_categories)).all()
            recommended_products += [p.name for p in category_products if p.name.lower() != food_name.lower()]

            # 去重处理
            recommended_products = list(set(recommended_products))

        # 提取推荐食物类别
        recommended_categories_str = analysis_result.split("4. 推荐食物类别：")[1].split("5. 饮食建议：")[0].strip()
        if recommended_categories_str == "无":
            recommended_categories = []
        else:
            recommended_categories = recommended_categories_str.split("、")

        # 根据推荐食物类别进一步筛选推荐商品
        for category in recommended_categories:
            category_products = Product.query.filter(Product.food_category.contains(category)).all()
            for product in category_products:
                if product.name not in recommended_products:
                    recommended_products.append(product.name)

        # 格式化分析结果
        formatted_result = f"""
1. 是否适合食用：{suitable}
2. 原因分析：{analysis_result.split("2. 原因分析：")[1].split("3. 推荐商品：")[0].strip()}
3. 推荐商品：{', '.join(recommended_products)}
4. 推荐食物类别：{recommended_categories_str}
5. 饮食建议：{analysis_result.split("5. 饮食建议：")[1].strip()}
"""
        return formatted_result, recommended_products, recommended_categories  # 确保返回三个值

    except Exception as e:
        return f"分析失败: {str(e)}", [], []


    # ... 已有代码 ...

@app.route('/check', methods=['POST'])
@login_required('user')
def check_food():
    user = User.query.get(session['user_id'])
    food_info = request.form.get('food', '')

    if 'image' in request.files:
        try:
            food_info = ocr_process(request.files['image'])
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    user_info = "\n".join([
        f"{d.disease_name}（过敏原：{d.allergies}，用药：{d.medications}）"
        for d in user.diseases
    ])

    analysis_result, recommended_products, recommended_categories = analyze_food(user_info, food_info)
    print(f"推荐商品列表：{recommended_products}")

    # 修改后的返回格式
    return jsonify({
        "analysis": analysis_result,
        "food_info": food_info,
        "recommended_products": [
            {
                "id": p.id,
                "name": p.name,
                "price": p.price  # 确保这里返回了价格字段
            }
            for p in Product.query.filter(Product.name.in_(recommended_products)).all()
        ],
        "recommended_categories": recommended_categories
    })


@app.route('/user/add-to-shopping-list', methods=['POST'])
@login_required('user')
def add_to_shopping_list():
    user = User.query.get(session['user_id'])
    product_name = request.form.get('product_name')
    if product_name:
        if user.shopping_list:
            user.shopping_list += f",{product_name}"
        else:
            user.shopping_list = product_name
        db.session.commit()
        return jsonify({"message": "商品已添加到购物清单"})
    return jsonify({"error": "商品名称不能为空"}), 400


@app.route('/user/shopping-list', methods=['POST'])
@login_required('user')
def shopping_list():
    user = User.query.get(session['user_id'])
    food_info = request.form.get('food', '')

    if 'image' in request.files:
        try:
            food_info = ocr_process(request.files['image'])
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    keywords = re.findall(r'\w+', food_info)
    query = Product.query
    for keyword in keywords:
        query = query.filter(Product.ingredients.contains(keyword))
    products = query.all()
    shopping_list = [p.name for p in products]
    return jsonify({"shopping_list": shopping_list})


# ... 已有代码 ...

# 添加获取商品配料信息的路由
@app.route('/api/product/ingredients/<int:product_id>', methods=['GET'])
@login_required('user')
def get_product_ingredients(product_id):
    product = Product.query.get(product_id)
    if product:
        return jsonify({"ingredients": product.ingredients})
    return jsonify({"error": "商品未找到"}), 404


# 修改删除购物清单商品的路由
@app.route('/user/remove-from-shopping-list', methods=['POST'])
@login_required('user')
def remove_from_shopping_list():
    user = User.query.get(session['user_id'])
    product_name = request.form.get('product_name')
    if product_name and product_name in user.shopping_list:
        if ',' in user.shopping_list:
            items = user.shopping_list.split(',')
            items = [item for item in items if item != product_name]
            user.shopping_list = ','.join(items)
        else:
            user.shopping_list = ''
        db.session.commit()
        return jsonify({"message": "商品已从购物清单中移除"})
    return jsonify({"error": "商品未找到或名称为空"}), 404


# ... 已有代码 ...

@app.route('/')
def index():
    return redirect(url_for('combined_login'))


import hashlib
import random
import time
import xml.etree.ElementTree as ET


def dict_to_xml(d):
    xml = "<xml>"
    for k, v in d.items():
        xml += f"<{k}>{v}</{k}>"
    xml += "</xml>"
    return xml


def xml_to_dict(xml_str):
    root = ET.fromstring(xml_str)
    return {child.tag: child.text for child in root}


@app.route('/clear_shopping_list', methods=['POST'])
@login_required('user')
def clear_shopping_list():
    user = User.query.get(session['user_id'])
    user.shopping_list = ''
    db.session.commit()
    return jsonify({"message": "购物车已清空"})


@app.route('/wxpay_notify', methods=['POST'])
def wxpay_notify():
    result = xml_to_dict(request.data)

    # 验证签名
    sign = result.pop('sign')
    sign_str = '&'.join([f"{k}={v}" for k, v in sorted(result.items())]) + f"&key={app.config['WX_API_KEY']}"
    calc_sign = hashlib.md5(sign_str.encode()).hexdigest().upper()

    if sign == calc_sign and result['result_code'] == 'SUCCESS':
        # 处理支付成功逻辑
        # 这里可以添加订单记录、发送通知等操作
        return dict_to_xml({'return_code': 'SUCCESS', 'return_msg': 'OK'})
    return dict_to_xml({'return_code': 'FAIL', 'return_msg': '签名失败'})

@app.route('/create_wx_payment')
@login_required('user')
def create_wx_payment():
    user = User.query.get(session['user_id'])
    if not user.shopping_list:
        return jsonify({"error": "购物清单为空"}), 400

    # 计算总金额（单位：分）
    items = Product.query.filter(Product.name.in_(user.shopping_list.split(','))).all()
    total_fee = int(sum(p.price * 100 for p in items))

    # 生成订单号
    out_trade_no = f"NA{int(time.time())}{random.randint(1000, 9999)}"

    # 构造请求参数
    params = {
        'appid': app.config['WX_APPID'],
        'mch_id': app.config['WX_MCHID'],
        'nonce_str': hashlib.md5(str(random.random()).encode()).hexdigest(),
        'body': 'NutriAdvisor购物清单',
        'out_trade_no': out_trade_no,
        'total_fee': total_fee,
        'spbill_create_ip': request.remote_addr,
        'notify_url': app.config['WX_NOTIFY_URL'],
        'trade_type': 'JSAPI',
        'openid': session.get('wx_openid')  # 需要提前获取用户openid
    }

    # 生成签名
    sign_str = '&'.join([f"{k}={v}" for k, v in sorted(params.items())]) + f"&key={app.config['WX_API_KEY']}"
    params['sign'] = hashlib.md5(sign_str.encode()).hexdigest().upper()

    # 发送支付请求
    response = requests.post('https://api.mch.weixin.qq.com/pay/unifiedorder',
                             data=dict_to_xml(params),
                             headers={'Content-Type': 'application/xml'})

    result = xml_to_dict(response.content)
    if result.get('return_code') == 'SUCCESS':
        # 生成前端支付参数
        pay_params = {
            'appId': app.config['WX_APPID'],
            'timeStamp': str(int(time.time())),
            'nonceStr': hashlib.md5(str(random.random()).encode()).hexdigest(),
            'package': f"prepay_id={result['prepay_id']}",
            'signType': 'MD5'
        }
        sign_str = '&'.join([f"{k}={v}" for k, v in sorted(pay_params.items())]) + f"&key={app.config['WX_API_KEY']}"
        pay_params['paySign'] = hashlib.md5(sign_str.encode()).hexdigest().upper()

        return jsonify(pay_params)
    else:
        return jsonify({"error": result.get('return_msg', '支付请求失败')}), 400




if __name__ == '__main__':
    app.run(debug=True)
